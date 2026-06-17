# Changelog

## [0.2.5] - 2026-06-17

- Fix invoice address/contact-person not resolving from contact_id


## [0.2.4] - 2026-05-21

- fix: omit paymentConditions so Lexware applies its own default


## [0.2.0] - 2026-04-09

### Changed
- Bumped FastMCP dependency to >=3.2.2
- Improved docstrings for update_contact and update_article (version-fetching pattern)
- Documented get_financial_overview months default and maximum
- Added disambiguation between list_expenses and list_vouchers

### Added
- Automated version bump and release CI via GitHub Actions
- CHANGELOG.md for tracking changes
