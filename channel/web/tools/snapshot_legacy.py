# encoding:utf-8
"""Check out the pre-split console next to the current one, for comparison.

    python channel/web/tools/snapshot_legacy.py [git-ref]

The frontend used to be three files: ``chat.html``, ``static/js/console.js``
and ``static/css/console.css``. This pulls that version out of git into
``channel/web/static/legacy/`` so ``python app.py -old`` can serve it against
the same backend, with the same sessions and history, to compare behaviour.

The snapshot is not committed - git already has the only copy that matters, and
a second one would just rot. It is in .gitignore; re-run this to recreate it,
and delete the directory when the comparison is done.

Only the three changed files actually differ. ``workspace.js``,
``doc-editor.js``, ``vendor/`` and ``logo.jpg`` are untouched by the split, but
the scripts are copied anyway so the snapshot stands on its own if they change
later. Vendored libraries and the logo are left pointing at the live copies:
they are large, pinned, and identical.
"""

import os
import re
import subprocess
import sys

WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(WEB))
OUT = os.path.join(WEB, "static", "legacy")

# repo-relative source -> path inside the snapshot
FILES = {
    "channel/web/chat.html": "chat.html",
    "channel/web/static/js/console.js": "js/console.js",
    "channel/web/static/js/workspace.js": "js/workspace.js",
    "channel/web/static/js/doc-editor.js": "js/doc-editor.js",
    "channel/web/static/css/console.css": "css/console.css",
}


def _show(ref, path):
    """Read one file out of a git ref, as bytes."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"cannot read {path} at {ref}:\n"
            f"  {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else "master"

    written = []
    for source, target in FILES.items():
        data = _show(ref, source)
        if target == "chat.html":
            text = data.decode("utf-8")
            # The snapshot's own scripts and stylesheet sit under
            # assets/legacy/. Relative refs resolve against /chat, which is
            # where -old serves this, so the prefix is all that changes.
            text, n = re.subn(r'assets/(js|css)/', r'assets/legacy/\1/', text)
            if not n:
                raise SystemExit("chat.html at this ref has no assets/js or "
                                 "assets/css references; wrong ref?")
            data = text.encode("utf-8")

        full = os.path.join(OUT, target)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        written.append((target, len(data)))

    rev = subprocess.run(["git", "rev-parse", "--short", ref], cwd=ROOT,
                         stdout=subprocess.PIPE).stdout.decode().strip()
    print(f"pre-split console from {ref} ({rev}) -> {os.path.relpath(OUT, ROOT)}")
    for target, size in written:
        print("  %-24s %8.1f KB" % (target, size / 1024))
    print("\nserve it with:  python app.py -old")


if __name__ == "__main__":
    main()
