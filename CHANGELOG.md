# Changelog

## [0.3.5] - 2026-05-29

- feat(CDI-1164): add structured purchase voucher creation for Belegfänger enrichment


## [0.3.4] - 2026-05-29

- fix: upload files as multipart/form-data (Lexoffice /files 500)


## [0.3.3] - 2026-05-25

- feat: add recurring template tools + invoice voucherType fix


## [0.3.2] - 2026-05-25

- fix(CDI-1160): query both salesinvoice and invoice voucherTypes


## [0.3.1] - 2026-05-25

- feat: v0.3.0 — capability tools, LLM ergonomics overhaul


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
