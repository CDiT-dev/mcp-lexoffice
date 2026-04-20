# Changelog

## [0.2.8] - 2026-04-20

- ci(deps): enable Dependabot weekly updates


## [0.2.7] - 2026-04-19

- refactor(config): add Settings with SecretStr, remove op:// subprocess


## [0.2.6] - 2026-04-18

- feat(reliability): stateless_http + /health + fail-fast + FastMCP 3.2.4


## [0.2.5] - 2026-04-16

- Add update_voucher tool for fixing VAT rates


## [0.2.4] - 2026-04-15

- Fix list_expenses: include paid status in default filter


## [0.2.3] - 2026-04-15

- Add get_voucher endpoint for VAT line item details


## [0.2.2] - 2026-04-09

- fix: lowercase Docker image tags in release CI


## [0.2.0] - 2026-04-09

### Changed
- Bumped FastMCP dependency to >=3.2.2
- Improved docstrings for update_contact and update_article (version-fetching pattern)
- Documented get_financial_overview months default and maximum
- Added disambiguation between list_expenses and list_vouchers

### Added
- Automated version bump and release CI via GitHub Actions
- CHANGELOG.md for tracking changes
