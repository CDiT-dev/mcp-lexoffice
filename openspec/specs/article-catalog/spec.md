# article-catalog Specification

## Purpose
TBD - created by archiving change mvp-scaffold. Update Purpose after archive.

## Requirements

### Requirement: List articles
The server SHALL provide a `list_articles` tool that returns all configured service items.

#### Scenario: List all articles
- **WHEN** `list_articles` is called
- **THEN** the tool returns all articles with name, type, net price, tax rate, and unit

### Requirement: Create article
The server SHALL provide a `create_article` tool for adding reusable service items.

#### Scenario: Create service article
- **WHEN** `create_article` is called with `name`, `type` (default "SERVICE"), `net_price`, `unit_name` (e.g., "Stunde", "Pauschal", "Tag"), and optional `description`
- **THEN** the tool creates the article in Lexoffice with `taxRatePercentage: 0` (Kleinunternehmer) and returns the article ID

### Requirement: Get article
The server SHALL provide a `get_article` tool.

#### Scenario: Get article by ID
- **WHEN** `get_article` is called with `article_id`
- **THEN** the tool returns the full article details

### Requirement: Update article
The server SHALL provide an `update_article` tool with optimistic locking.

#### Scenario: Update article price
- **WHEN** `update_article` is called with `article_id`, `version`, and updated fields
- **THEN** the tool updates the article and returns the new version number

#### Scenario: Version conflict
- **WHEN** `update_article` is called with a stale `version` number
- **THEN** the tool returns an error indicating a version conflict (HTTP 409)

### Requirement: Service catalog resource
The server SHALL expose the configured service catalog (from `LEXOFFICE_SERVICE_CATALOG`) at `lexoffice://service-catalog` for Claude to use when creating articles.

#### Scenario: Claude knows the service offerings
- **WHEN** Claude is asked to create an invoice for a catalog service
- **THEN** Claude can read `lexoffice://service-catalog` for the service names and rates
