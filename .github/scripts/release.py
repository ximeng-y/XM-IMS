"""版本 tag 与 Release 衔接：构建前校验 tag，构建后向同 tag Release 挂安装包。

用法：
    python -B .github/scripts/release.py prepare --tag v0.1.1
    python -B .github/scripts/release.py publish --tag v0.1.1 \
        --repo ximeng-y/XM-IMS --asset <.XMTEMP/electron-windows/dist/*.exe>

prepare 把 version 与 asset_path 写入 GITHUB_OUTPUT；publish 只从 GH_TOKEN
环境变量读取凭据，本脚本不导入业务应用、不启动构建。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
API_URL = "https://api.github.com/graphql"
TAG_PATTERN = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+")
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

# isDraft 与 immutable 同时覆盖草稿、正式发布与不可变状态；REST 的
# /releases/tags/{tag} 查不到草稿，不能作为存在性依据。
RELEASE_QUERY = """
query($owner: String!, $name: String!, $tag: String!) {
  repository(owner: $owner, name: $name) {
    release(tagName: $tag) {
      databaseId
      tagName
      isDraft
      immutable
    }
  }
}
"""


def fail(message):
    print(f"发布失败：{message}", file=sys.stderr)
    raise SystemExit(1)


def app_version():
    """唯一应用版本来源：app/version.py 的 APP_VERSION。"""
    version_file = PROJECT_ROOT / "app" / "version.py"
    namespace = {}
    source = version_file.read_text(encoding="utf-8")
    exec(compile(source, str(version_file), "exec"), namespace)
    version = str(namespace.get("APP_VERSION") or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        fail(f"APP_VERSION 不是 X.Y.Z 格式：{version!r}")
    return version


def normalized_tag(tag):
    """校验 tag 形状并返回去掉可选 v 的版本；原 tag 原样用于查询与发布。"""
    if not TAG_PATTERN.fullmatch(tag):
        fail(f"tag 不是 vX.Y.Z 或 X.Y.Z 格式：{tag!r}")
    return tag[1:] if tag.startswith("v") else tag


def asset_name(version):
    return f"XM-IMS-{version}-win-x64-Setup.exe"


def check_tag_version(tag):
    """tag、APP_VERSION 与安装包文件名共用这一处版本规则。"""
    version = normalized_tag(tag)
    expected = app_version()
    if version != expected:
        fail(f"tag {tag!r} 与 APP_VERSION {expected!r} 不一致，不进入构建与发布")
    return version


def write_output(key, value):
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(f"{key}={value}\n")
    print(f"{key}={value}")


def prepare(tag):
    version = check_tag_version(tag)
    asset_path = PROJECT_ROOT / ".XMTEMP" / "electron-windows" / "dist" / asset_name(version)
    write_output("version", version)
    write_output("asset_path", str(asset_path))
    print(f"版本 {version} 与 tag {tag} 一致，安装包将输出到 {asset_path}")


def split_repo(repo):
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name or "/" in name:
        fail(f"仓库名不是 owner/name 格式：{repo!r}")
    return owner, name


def find_release(token, owner, name, tag):
    """返回同 tag 的 Release 对象；确认不存在时返回 None，其余异常直接失败。"""
    payload = json.dumps({
        "query": RELEASE_QUERY,
        "variables": {"owner": owner, "name": name, "tag": tag},
    }).encode("utf-8")
    request = urllib.request.Request(API_URL, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "XM-IMS-Release",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        fail(f"Release 查询失败：HTTP {error.code}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        fail(f"Release 查询失败：{error}")
    if body.get("errors"):
        messages = "；".join(str(item.get("message", item)) for item in body["errors"])
        fail(f"Release 查询失败：{messages}")
    repository = (body.get("data") or {}).get("repository")
    if repository is None:
        fail("仓库不可见或不存在，无法确认 Release 状态")
    if "release" not in repository:
        fail("Release 查询结果缺少 release 字段")
    return repository["release"]


def run_gh(arguments):
    """GitHub CLI 通过参数列表调用，不拼接 shell 字符串，退出码向工作流传播。"""
    try:
        result = subprocess.run(
            ["gh"] + arguments, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as error:
        fail(f"无法运行 GitHub CLI：{error}")
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        fail(f"GitHub CLI 执行失败：{detail}")


def upload_asset(tag, repo, asset_path):
    # 不带 --clobber：同名资产已存在时由 CLI 报错终止，不覆盖、不改写原资产
    run_gh(["release", "upload", tag, str(asset_path), "--repo", repo])


def create_draft(tag, repo, asset_path):
    # 只关联已推送的 tag（--verify-tag），不自动生成新 tag；说明留空供用户填写
    run_gh([
        "release", "create", tag, str(asset_path), "--repo", repo,
        "--draft", "--verify-tag", "--title", tag, "--notes", "",
    ])


def publish(tag, repo, asset):
    version = check_tag_version(tag)
    asset_path = Path(asset)
    if asset_path.name != asset_name(version):
        fail(f"安装包文件名 {asset_path.name!r} 不符合产物约定 {asset_name(version)!r}")
    if not asset_path.is_file():
        fail(f"安装包不存在：{asset_path}")
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        fail("缺少 GH_TOKEN 环境变量")
    owner, name = split_repo(repo)
    release = find_release(token, owner, name, tag)
    if release is None:
        create_draft(tag, repo, asset_path)
        print(f"已创建草稿 Release {tag} 并挂载 {asset_path.name}，正式发布由用户完成")
        return
    if release.get("immutable"):
        fail("该 Release 已不可变，正式发布前需要先挂齐资产，无法再补传安装包")
    upload_asset(tag, repo, asset_path)
    print(f"已向 Release {tag}（草稿：{bool(release.get('isDraft'))}）挂载 {asset_path.name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="XM-IMS 版本 tag 与 Release 衔接")
    operations = parser.add_subparsers(dest="operation", required=True)

    prepare_parser = operations.add_parser("prepare", help="校验 tag 并输出 version/asset_path")
    prepare_parser.add_argument("--tag", required=True)

    publish_parser = operations.add_parser("publish", help="向同 tag Release 挂载安装包")
    publish_parser.add_argument("--tag", required=True)
    publish_parser.add_argument("--repo", required=True)
    publish_parser.add_argument("--asset", required=True)

    args = parser.parse_args(argv)
    if args.operation == "prepare":
        prepare(args.tag)
    else:
        publish(args.tag, args.repo, args.asset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
