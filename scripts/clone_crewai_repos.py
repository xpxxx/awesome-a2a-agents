#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


CREWAI_ANCHOR = '### <a name="CrewAI"></a>CrewAI'
NEXT_SECTION_PREFIX = '### '


def read_file_text(path: Path) -> str:
    with path.open('r', encoding='utf-8') as f:
        return f.read()


def extract_crewai_repo_urls(readme_text: str) -> list[str]:
    lines = readme_text.splitlines()

    try:
        start_idx = next(i for i, line in enumerate(lines) if line.strip() == CREWAI_ANCHOR)
    except StopIteration:
        raise RuntimeError('CrewAI section not found in README.md')

    repo_urls: list[str] = []
    for line in lines[start_idx + 1:]:
        if line.startswith(NEXT_SECTION_PREFIX):
            break
        if not line.strip().startswith('-'):
            continue
        # Extract first markdown link URL in the line
        match = re.search(r"\((https?://github\.com/[^)]+)\)", line)
        if match:
            url = match.group(1).strip()
            repo_urls.append(url)

    # de-duplicate while preserving order
    seen = set()
    deduped = []
    for url in repo_urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def owner_repo_from_url(url: str) -> tuple[str, str]:
    # e.g., https://github.com/owner/repo or with trailing parts
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", url)
    if not m:
        raise ValueError(f'Unrecognized GitHub URL: {url}')
    return m.group(1), m.group(2)


def clone_repo(url: str, dest_dir: Path, shallow: bool = True, skip_existing: bool = True) -> None:
    owner, repo = owner_repo_from_url(url)
    target_dir = dest_dir / f"{owner}-{repo}"

    if target_dir.exists():
        if skip_existing:
            print(f"[skip] {target_dir} already exists")
            return
        else:
            # Attempt to update existing repo
            print(f"[pull] {target_dir}")
            try:
                subprocess.run(
                    [
                        'git', '-C', str(target_dir), 'pull', '--ff-only'
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"[warn] git pull failed for {url}: {e}")
            return

    cmd = ['git', 'clone']
    if shallow:
        cmd += ['--depth', '1']
    cmd += [url, str(target_dir)]

    print(f"[clone] {url} -> {target_dir}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[error] git clone failed for {url}: {e}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description='Clone all CrewAI repos listed in README.md')
    parser.add_argument('--readme', type=Path, default=Path('README.md'), help='Path to README.md')
    parser.add_argument('--dest', type=Path, default=Path('crewai-repos'), help='Destination directory for clones')
    parser.add_argument('--no-shallow', action='store_true', help='Disable shallow clone (clone full history)')
    parser.add_argument('--update-existing', action='store_true', help='git pull if repo directory already exists')
    args = parser.parse_args(argv)

    readme_path = args.readme.resolve()
    dest_dir = args.dest.resolve()

    if not readme_path.exists():
        print(f"README not found: {readme_path}", file=sys.stderr)
        return 1

    dest_dir.mkdir(parents=True, exist_ok=True)

    readme_text = read_file_text(readme_path)
    urls = extract_crewai_repo_urls(readme_text)

    if not urls:
        print('No GitHub URLs found under CrewAI section', file=sys.stderr)
        return 2

    print(f"Found {len(urls)} CrewAI repos")
    for url in urls:
        clone_repo(
            url=url,
            dest_dir=dest_dir,
            shallow=not args.no_shallow,
            skip_existing=not args.update_existing,
        )

    print('Done.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))



