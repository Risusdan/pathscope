# PathScope M5: Rename, Packaging, CI, GitHub

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the tool as "PathScope": product rename, MIT license,
README with screenshots, PyInstaller onedir packaging, GitHub Actions
(tests + Windows exe on tag), private GitHub repo `pathscope` ready to
flip public.

**Architecture:** No engine/UI behavior changes. Rename touches metadata
and user-facing strings only. Packaging wraps the existing `gui`
subcommand; `targets/` ships OUTSIDE the bundle per spec section 3. CI
runs the suite headless (offscreen) on Linux and Windows; a tag build
produces the Windows onedir zip.

**Tech Stack:** existing project + PyInstaller>=6, GitHub Actions, gh CLI.

**Spec:** `docs/superpowers/specs/2026-09-12-data-path-explorer-design.md`
(section 3 Deployment Form is the binding authority for packaging).

## Global Constraints

- Python 3.9 syntax; pure ASCII in all source files.
- Product name is "PathScope" in user-facing surfaces (pyproject name,
  window title, README, CLI prog). The local directory name and the spec
  history keep their old wording - do NOT rewrite historical docs
  (specs/plans keep their filenames and contents; only forward-looking
  docs change).
- `prototype/ui_proto.py` is a frozen historical reference: untouched.
- MIT license, copyright line exactly: `Copyright (c) 2026 ChienLinSu`.
- The public repo stays generic: no company identifiers anywhere
  (existing constraint; re-verify before push).
- Commit after every task, Conventional Commits.
- All commands from repo root; python/pytest via `.venv/bin/`.

---

### Task 1: Product rename to PathScope

**Files:**
- Modify: `pyproject.toml`, `cli.py`, `ui/app.py` and/or
  `ui/main_window.py` (window title), `README.md` (title + name uses)
- Test: `tests/test_cli.py` (prog name), full suite

**Interfaces:**
- Produces: `[project] name = "pathscope"`, description "Live data-path
  observer for MCU/SoC bring-up and debugging"; console script
  `pathscope = cli:main` under `[project.scripts]`; argparse
  `prog="pathscope"`; window title "PathScope"; README title
  "# PathScope". `python -m cli` keeps working (back-compat).

- [ ] **Step 1:** Update pyproject.toml: name, description, add

```toml
[project.scripts]
pathscope = "cli:main"
```

and extend `[tool.setuptools]` with `py-modules = ["cli"]` so the
console script resolves in non-editable installs.
- [ ] **Step 2:** cli.py: `prog="pathscope"`. ui window title:
  "PathScope". README: retitle and reword first paragraph to introduce
  PathScope (keep validation sections).
- [ ] **Step 3:** Add to tests/test_cli.py:

```python
def test_prog_name_is_pathscope():
    assert cli.build_parser().prog == "pathscope"
```

- [ ] **Step 4:** `.venv/bin/pip install -e ".[ui,dev]"` then verify
  `.venv/bin/pathscope gui --demo --shot /tmp/rename.png` runs and the
  PNG's title bar area is fine (Read it); full suite green.
- [ ] **Step 5:** Commit: `feat: rename product to PathScope`

---

### Task 2: MIT LICENSE

**Files:**
- Create: `LICENSE`

- [ ] **Step 1:** Standard MIT text, line 3:
  `Copyright (c) 2026 ChienLinSu`
- [ ] **Step 2:** Reference it from pyproject:
  `license = {file = "LICENSE"}` (PEP 621 table form for setuptools>=61
  compatibility on Python 3.9 toolchain).
- [ ] **Step 3:** `.venv/bin/pip install -e ".[ui,dev]"` still clean;
  commit: `chore: add MIT license`

---

### Task 3: PyInstaller onedir packaging

**Files:**
- Create: `packaging/pathscope.spec`, `packaging/build.sh` (POSIX),
  `packaging/build.ps1` (Windows), `packaging/README.md`
- Modify: `pyproject.toml` (add `build = ["pyinstaller>=6.0"]` extra)

**Interfaces:**
- Produces: `packaging/build.sh` runs PyInstaller with the spec and
  assembles `dist/pathscope/` (the onedir bundle) plus a SIBLING
  `targets/` directory copied next to the executable, then zips
  `dist/pathscope-<version>-<os>.zip` containing both. Spec section 3:
  targets/ lives NEXT TO the executable, never inside the bundle.
- Entry point: a tiny `packaging/entry.py` that calls
  `sys.exit(cli.main(["gui"] + sys.argv[1:]))` so the exe defaults to
  the GUI but forwards flags (`--demo`, `--target-dir`).
- Hidden imports: PySide6 hooks are automatic; add pyocd's dynamic
  probe plugins if the build warns (document findings in
  packaging/README.md).

- [ ] **Step 1:** Write entry.py, the .spec (onedir, name "pathscope",
  console=False on Windows/console=True elsewhere is a per-OS wrinkle -
  keep console=True everywhere for MVP so errors are visible; note it),
  build.sh/build.ps1 (install build extra, run pyinstaller, copy
  targets/, zip).
- [ ] **Step 2:** Run `packaging/build.sh` locally (macOS). Verify:
  `dist/pathscope/pathscope --demo --shot /tmp/pkg.png` works from the
  bundle (offscreen), PNG sane (Read it), targets/ sits beside the
  executable, zip produced.
- [ ] **Step 3:** Full suite still green (packaging adds no runtime
  code). Commit: `feat(packaging): pyinstaller onedir bundle scripts`

---

### Task 4: GitHub Actions - CI and release

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/release.yml`

**Interfaces:**
- ci.yml: on push/PR to main - matrix {ubuntu-latest, windows-latest},
  Python 3.9 and 3.12; `pip install -e ".[ui,dev]"`;
  `QT_QPA_PLATFORM=offscreen pytest -q` (env set at job level; hw tests
  are deselected by default already). Ubuntu needs the Qt xkb system
  libs: install `libegl1 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0
  libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0` via
  apt before running (document: offscreen still needs libegl/xkb).
- release.yml: on tag `v*` - windows-latest, run packaging/build.ps1,
  upload the zip as a release asset (softprops/action-gh-release or
  `gh release upload`; use the official `gh` since it ships on
  runners).

- [ ] **Step 1:** Write both workflows.
- [ ] **Step 2:** Local lint: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); yaml.safe_load(open('.github/workflows/release.yml'))"`.
  (Real validation happens on first push - note in report.)
- [ ] **Step 3:** Commit: `ci: test matrix and windows release build`

---

### Task 5: README polish with screenshots

**Files:**
- Create: `docs/img/pathscope-demo.png`, `docs/img/pathscope-inspector.png`
- Modify: `README.md`

- [ ] **Step 1:** Generate fresh shots with the renamed build:
  `pathscope gui --demo --shot docs/img/pathscope-demo.png` and
  `--shot docs/img/pathscope-inspector.png --shot-select dma2`
  (use `python -m ui.app` if the cli passthrough gap is still open).
  Read both PNGs - they are the repo's face; verify clean.
- [ ] **Step 2:** Rework README: hero paragraph (what/why in 3
  sentences), the two screenshots, Features list (live diagram, rules,
  guarded-register safety, inspector, memory viewer, demo mode),
  Quick start (pip install -e, pack install stm32f411ce, pathscope gui
  --demo / real), Adding your own target (3-file recipe with a short
  yaml sample), Hardware validation summary (keep existing sections,
  condensed), License badge/line. Public-repo generic wording.
- [ ] **Step 3:** Commit: `docs: README for public release with screenshots`

---

### Task 6: GitHub repo creation and push (CONTROLLER + USER)

- [ ] **Step 1:** `gh auth status` - if not logged in, user runs
  `gh auth login` themselves (suggest `! gh auth login` in the prompt).
- [ ] **Step 2:** `gh repo create pathscope --private --source . --push`
  (personal account, private per decision; confirm remote `origin` set
  and branch main pushed).
- [ ] **Step 3:** Verify ci.yml runs green on GitHub (gh run watch).
  Fix-forward any CI-environment-only failures (apt packages, paths) as
  `ci:` commits.
- [ ] **Step 4:** Report the repo URL. Flip-to-public stays a USER
  action for later (`gh repo edit --visibility public`), after their
  final look.

---

## After this plan

- Tag `v0.1.0` when the user says go: release.yml produces the Windows
  zip (M5 exit criterion).
- M6 candidate: scope view. M7: trace-buffer ingestion.
