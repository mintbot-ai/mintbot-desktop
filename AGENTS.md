# mintbot Desktop - agent onboarding

mintbot Desktop is an AXP extension published by mintbot.ai and developed with
the mintbot platform's package flow. That flow is written up once, in the
mintbot repository: `developer_tools/agent_skills/axp_package_release.md`.
Read it before cutting or promoting a release.

In short:

- `main` is the dev channel. Every push publishes a signed dev build
  (`scripts/release-dev.sh`, run by `.github/workflows/dev-build.yml`) that only
  DEV agents can install; LIVE agents never see one.
- DEV's Beaver "AXP package tests" (CatOffice) install the second-newest dev
  build and upgrade it to the newest, driving the real panel mount and desktop.
  A feature is not done until that journey (`beaver/journeys/axp_packages/
  test_mintbot_desktop.py` in the mintbot repository) covers it.
- A release on the package's own channel (`beta`, what LIVE agents install)
  promotes a dev build that journey passed on: `scripts/release.sh` refuses
  without `AXP_PROMOTE_FROM=<dev build>` and the mintbot repository's promote
  gate (`developer_tools/axp_promote_check.py`).
- Code, comments and commit messages are English.
- `python -m pytest` before every push; `tests/test_release_channels.py` runs
  the release scripts against fakes and never touches a key or GitHub.
