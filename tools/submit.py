#!/usr/bin/env python3
"""Submit one or both website scenarios to the GMT cluster, for one URL or a list.

This is a thin wrapper around gmt-helpers' submit_software.py. It exists so that
the things that are easy to get wrong in a 1000-site sweep are not retyped 2000
times:

  * the repository URL, which must be spelled WITHOUT the .git suffix, because
    energy-ID tasks match runs.uri exactly rather than with LIKE, and the runs
    table holds both spellings. The wrong one silently groups with nothing.
  * --branch and --filename, which argparse in submit_software.py calls optional
    and the API's Pydantic model does not.
  * the machine, because runs on different machines are not comparable and the
    two scenarios here are only worth anything against each other.
  * the run name, which is what the dashboard shows and therefore has to
    identify the scenario AND the site.

Examples:

    # one site, both scenarios
    tools/submit.py --url https://www.green-coding.io

    # one site, one scenario, see the command without sending it
    tools/submit.py --url https://www.green-coding.io --scenario parrot --dry-run

    # the sweep: one file, one URL per line, blank lines and # comments ignored
    tools/submit.py --url-file sites.txt
"""
import argparse
import shlex
import subprocess
import sys
from pathlib import Path

# Without .git. See the module docstring.
REPO_URL = 'https://github.com/green-coding-solutions/1000-websites'

DEFAULT_MACHINE_ID = 6

SCENARIOS = {
    'playwright': {
        'filename': 'playwright/usage_scenario.yml',
        'label': 'Playwright Chrome',
    },
    'parrot': {
        'filename': 'parrot/usage_scenario.yml',
        'label': 'Parrot Chrome',
    },
}

SUBMIT_SCRIPT = Path('/home/didi/code/gmt-helpers/api/submit_software.py')
# submit_software.py needs `requests`, which is in the Green Metrics Tool venv.
GMT_PYTHON = Path('/home/didi/code/green-metrics-tool/venv/bin/python')


def run_name(scenario, url):
    """The job name the dashboard shows.

    Deliberately the same shape as the `name:` field inside each
    usage_scenario.yml, which renders to "<page> - 1000 Websites <driver>
    Chrome" once __GMT_VAR_PAGE__ is substituted. Keeping the two identical
    means the job and the run it produces carry the same name, instead of two
    spellings of the same thing that have to be reconciled by eye when reading
    2000 rows.
    """
    return f"{url} - 1000 Websites {SCENARIOS[scenario]['label']}"


def read_urls(path):
    urls = []
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            urls.append(line)
    return urls


def build_command(scenario, url, args):
    spec = SCENARIOS[scenario]
    cmd = [
        str(GMT_PYTHON), str(SUBMIT_SCRIPT), 'submit',
        '--name', run_name(scenario, url),
        '--repo-url', REPO_URL,
        '--branch', args.branch,
        '--filename', spec['filename'],
        '--machine-id', str(args.machine_id),
        '--schedule-mode', args.schedule_mode,
        '--variables', f'__GMT_VAR_PAGE__={url}',
    ]
    if args.email:
        cmd += ['--email', args.email]
    return cmd


def main():
    p = argparse.ArgumentParser(
        description='Submit the website scenarios to the GMT cluster.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument('--url', help='A single page to measure.')
    src.add_argument('--url-file', help='File with one URL per line; blank lines and # comments ignored.')
    p.add_argument('--scenario', choices=['playwright', 'parrot', 'both'], default='both',
                   help='Which scenario to submit (default: both).')
    p.add_argument('--branch', default='main', help='Branch the cluster clones (default: main).')
    p.add_argument('--machine-id', type=int, default=DEFAULT_MACHINE_ID,
                   help=f'Target machine (default: {DEFAULT_MACHINE_ID}, the GUI machine the sweep runs on).')
    p.add_argument('--schedule-mode', default='one-off',
                   help='Schedule mode passed through to the API (default: one-off).')
    p.add_argument('--email', help='Optional address to notify on completion.')
    p.add_argument('--dry-run', action='store_true', help='Print the commands instead of running them.')
    args = p.parse_args()

    urls = [args.url] if args.url else read_urls(args.url_file)
    if not urls:
        p.error('no URLs to submit')

    scenarios = list(SCENARIOS) if args.scenario == 'both' else [args.scenario]

    if not args.dry_run:
        for path in (SUBMIT_SCRIPT, GMT_PYTHON):
            if not path.exists():
                sys.exit(f'not found: {path}')

    failures = []
    for url in urls:
        for scenario in scenarios:
            cmd = build_command(scenario, url, args)
            if args.dry_run:
                # shlex.join, not ' '.join: the run name contains spaces, and an
                # unquoted line that looks copy-pasteable but is not is worse
                # than no line at all.
                print(shlex.join(cmd))
                continue
            print(f'--> {scenario}: {url}', flush=True)
            if subprocess.run(cmd, check=False).returncode != 0:
                # Keep going. In a sweep, one rejected submission should not
                # cost the other 999.
                failures.append((scenario, url))

    if failures:
        print(f'\n{len(failures)} submission(s) FAILED:', file=sys.stderr)
        for scenario, url in failures:
            print(f'  {scenario}  {url}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
