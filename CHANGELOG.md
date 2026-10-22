# Changelog

## 1.4.0

- Require explicit RU/EN selection after `/start` before opening the menu or
  checking subscription access.
- Add separate language-specific broadcast drafts, audience previews and history;
  preserve existing running recipient snapshots during database upgrades.
- Show unique-user statistics and broadcast audiences for RU and EN separately.
- Return to the same button settings after saving or cancelling URL/label edits.
- Add persistent default, blue, green and red link button colors in the admin menu.

## 1.3.0

- Publish the project as URL Bot under the MIT license.
- Use the `url-bot` package, command, service and dedicated system account.
- Remove the built-in repository URL; source updates use your configured origin
  or `BOT_REPO_URL`.
- Add source builds, build provenance, package checksums and GitHub workflows.
- Preserve five configurable links, RU/EN menus, statistics, subscription checks,
  persistent broadcasts and the SSH administration menu.
