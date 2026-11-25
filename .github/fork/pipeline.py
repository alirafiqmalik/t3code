#!/usr/bin/env python3
"""Release bookkeeping. Invoked only from the trusted fork-ci checkout."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

FORK_TAG_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)-fork\.(\d{8})\.(\d+)$')
SEMVER_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$')


def output(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def fork_tag_sort_key(tag_name):
    match = FORK_TAG_RE.fullmatch(tag_name)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def compute_next_fork_version(base_version, date_str, existing_tags):
    match = SEMVER_RE.fullmatch(base_version.strip().lstrip('v'))
    if not match:
        raise ValueError(f'Unsupported package version: {base_version}')
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    target_core = f'{major}.{minor}.{patch + 1}'
    prefix = f'v{target_core}-fork.{date_str}.'
    max_seq = 0
    for tag in existing_tags:
        if tag.startswith(prefix):
            tail = tag[len(prefix):]
            if tail.isdigit():
                max_seq = max(max_seq, int(tail))
    return f'{target_core}-fork.{date_str}.{max_seq + 1}'


def list_remote_fork_tags(repo):
    raw = output('git', '-C', repo, 'ls-remote', '--tags', 'origin', 'refs/tags/v*-fork.*')
    tags = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if ref.endswith('^{}'):
            continue
        if ref.startswith('refs/tags/'):
            tags.append(ref.removeprefix('refs/tags/'))
    return tags


def prepare(report, repo, composed_dir, force, build_desktop):
    data = json.loads(report.read_text())
    data['controller_sha'] = output('git', '-C', repo, 'rev-parse', 'HEAD')
    controller_files = output(
        'git', '-C', repo, 'ls-tree', '-r', 'HEAD', '--',
        '.github/fork/stack.py', '.github/fork/pipeline.py', '.github/workflows/fork-build.yml'
    )
    data['build_fingerprint'] = hashlib.sha256(
        (data['fingerprint'] + controller_files).encode()
    ).hexdigest()
    data['build_desktop'] = bool(build_desktop)
    previous_ref = output('git', '-C', repo, 'ls-remote', 'origin', 'refs/heads/build/local')
    data['previous_build_sha'] = previous_ref.split()[0] if previous_ref else ''

    repository = os.environ['GITHUB_REPOSITORY']
    releases = json.loads(
        output('gh', 'api', '--paginate', '--slurp', f'repos/{repository}/releases?per_page=100')
    )
    all_releases = [release for page in releases for release in page]
    published = [
        release for release in all_releases
        if not release.get('draft') and fork_tag_sort_key(release.get('tag_name', '')) is not None
    ]
    latest = max(published, key=lambda r: fork_tag_sort_key(r['tag_name']), default=None)

    should_build = True
    if latest:
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                ['gh', 'release', 'download', latest['tag_name'], '--repo', repository,
                 '--pattern', 'build-manifest.json', '--dir', temporary],
                check=True,
            )
            old = json.loads((Path(temporary) / 'build-manifest.json').read_text())
            same_fingerprint = old.get('build_fingerprint') == data['build_fingerprint']
            desktop_satisfied = (not build_desktop) or bool(old.get('build_desktop'))
            should_build = force or (not same_fingerprint) or (not desktop_satisfied)

    source_root = Path(composed_dir or repo)
    desktop_pkg = json.loads((source_root / 'apps' / 'desktop' / 'package.json').read_text())
    run_date = datetime.now(timezone.utc).strftime('%Y%m%d')
    existing_tags = set(list_remote_fork_tags(repo))
    for release in all_releases:
        if release.get('tag_name'):
            existing_tags.add(release['tag_name'])

    version = compute_next_fork_version(desktop_pkg['version'], run_date, sorted(existing_tags))
    tag = f'v{version}'
    data['version'] = version
    data['tag'] = tag
    data['release_name'] = f'T3 Code Fork {version}'
    data['run_url'] = f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    report.write_text(json.dumps(data, indent=2) + '\n')

    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f"should_build={str(should_build).lower()}\n")
        stream.write(f"build_desktop={str(bool(build_desktop)).lower()}\n")
        for key in ('composed_sha', 'version', 'tag', 'release_name'):
            stream.write(f'{key}={data[key]}\n')

    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(f"Upstream: `{data['upstream']}`\n\nComposed: `{data['composed_sha']}`\n\n")
        stream.write(f"Target version: `{version}` (`{tag}`)\n\n")
        for entry in data['patches']:
            stream.write(f"- `{entry['branch']}` @ `{entry['head']}`\n")
        for entry in data['retired']:
            stream.write(f"- Integrated upstream: `{entry['branch']}` (PR #{entry['pr']})\n")
        if not should_build:
            stream.write('\nSource and pipeline unchanged; keeping the existing release.\n')


def notes(report, destination):
    data = json.loads(report.read_text())
    repository = os.environ.get('GITHUB_REPOSITORY', 'alirafiqmalik/t3code')
    lines = [
        f"T3 Code Fork `{data['version']}`",
        '',
        'Platforms:',
        '- CLI runtime archive: `linux-x64` (`t3-{0}-linux-x64.tar.gz`)'.format(data['version']),
        '- CLI runtime archive: `darwin-arm64` (`t3-{0}-darwin-arm64.tar.gz`)'.format(data['version']),
    ]
    if data.get('build_desktop'):
        lines += [
            '- Desktop packages: Linux x64 AppImage + macOS arm64 DMG/ZIP',
        ]
    lines += [
        '',
        'Install / upgrade CLI:',
        '```sh',
        f"curl -fsSL https://raw.githubusercontent.com/{repository}/{data['tag']}/scripts/install.sh | sh",
        '```',
        '',
        f"Upstream: `{data['upstream']}`",
        f"Source: `{data['composed_sha']}`",
        f"Version: `{data['version']}`",
        '',
        'Included fork branches:',
        '',
    ]
    lines += [f"- `{p['branch']}` at `{p['head']}`" for p in data['patches']]
    lines += ['', f"Build: {data['run_url']}", '',
              '`SHA256SUMS` and `build-manifest.json` are attached.', '']
    destination.write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'notes'])
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--composed-dir', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--build-desktop', action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.report, args.repo, args.composed_dir, args.force, args.build_desktop)
    else:
        notes(args.report, args.destination)


if __name__ == '__main__':
    main()
