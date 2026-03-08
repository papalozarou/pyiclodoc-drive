# Coding standards

This file defines various coding standards for agents to adhere to.

## Scope and precedence
- MUST: Treat this file as the default project policy for all files in this repository.
- MUST: If instructions conflict, ask for clarification before editing files.

## Agent workflow
- MUST: Never assume — ask for clarification when requirements are unclear
- MUST: Until told otherwise, detail suggestions before any live edits
- MUST: Ask for explicit permission before making edits
- MUST NOT: Make file edits before explicit user approval
- MUST: Review existing files and patterns before proposing any change
- MUST: Propose minimal, standards-compliant edits first, then ask for permission
- MUST: After approval, apply focused edits and avoid unrelated changes
- MUST: Summarise what changed and suggest updates to this file when useful patterns emerge
- MUST: Ensure the repository is initialised with Git and has at least one baseline commit before any coding changes begin
- MUST: If no baseline commit exists, stop and request user approval to create an initial commit before proceeding with code edits
- MUST: After each block of suggested and user-approved changes is applied, explicitly suggest a commit (with a clear commit message) before moving to the next change block
- MUST: When a new rule is requested, ask whether it should be project-local (`AGENTS.local.md`) or added to the shared template (`AGENTS.md`) before editing rules
- SHOULD: If the rule is project-local, suggest creating or updating `AGENTS.local.md` and ensure it is ignored by baseline ignore files

## README.md and comments
- MUST: Use UK English, not US
- MUST: Ensure a README.md exists
- MUST: Match the writing style from available examples in my [GitHub repository](https://github.com/papalozarou) and [blog](https://lozworld.com)
- MUST: If available examples are insufficient to infer style confidently, ask for a sample before large edits
- MUST: Use the same syntax and formatting for comments as in my other GitHub projects
- MUST: Write verbose, highly structured, comments that would enable debugging at 2am
- SHOULD: Add source links for non-obvious logic (for example Stack Overflow or official documentation)
- MUST: Add an overview comment at the top, or near the top, of source files describing what the file does
- MUST: Add comments directly above functions and methods, describing purpose, arguments, return values, and notable caveats
- MUST: Wrap every comment block with separator lines above and below
- MUST: A separator line must use the language comment prefix, one space, then hyphens filling the remainder of the line to the 80-character limit
- MUST: Use sentence-case prose in comments with full sentences and terminal punctuation
- MUST: For function comments include purpose, numbered arguments, behaviour notes, and failure or exit behaviour where relevant
- MUST: Do not add inline comments inside function or method bodies unless external constraints require it
- MUST: When referring to arguments, parameters, flags, or equivalent inputs in prose or comments, use double quotes instead of single-quoted identifiers

Examples:
```text
"${1:?}"
"${2:-default}"
"--flag"
"PARAM_NAME"
```
- SHOULD: Include an `N.B.` subsection for caveats, constraints, or gotchas where relevant
- SHOULD: Add source links for non-obvious logic as a short bullet list under the comment block
- MUST: Use consistent separator and heading phrasing across files (for example `Imported ...`, `Run the script.`, `Functions for ...`)
- MUST: Use paragraph spacing in comments, including blank comment lines between intro, argument lists, `N.B.`, and notes or reference sections
- MUST: Keep all comment lines at or below 80 characters, including separator lines

## Coding
- MUST: Prefer DRY standards
- MUST NOT: Use nested for-loops or nested if-statements
- MUST: Prefer separation of concerns when writing functions and scripts
- MUST: Follow recognised best-practice coding standards
- MUST: Ensure logic is clear, robust, and easy to reason about
- MUST: Prefer readability and maintainability over clever or overly complex implementations
- MUST: Where practical, design and implement code as a coherent system, not isolated patches
- SHOULD: Where practical, favour extensible designs that allow safe future change without large rewrites
- MUST NOT: Use quick-fix, hacky, or temporary code in final changes
- MUST: Implement production-ready solutions with explicit error handling and clear control flow
- MUST: Refactor instead of layering workarounds when existing code structure is weak
- MUST: Remove dead code, debug artefacts, and commented-out code before completion
- SHOULD: Use TDD where practical
- MUST: Add generic, project-relevant ignore files (for example git and docker), while keeping AGENTS.md tracked for submodule-based reuse across projects
- MUST: Proactively suggest an appropriate licence for each new repository based on intended usage (for example MIT, Apache-2.0, GPL-3.0-only, or proprietary)
- MUST: If no licence choice is provided, ask for confirmation before adding any licence file
- MUST: Use `compose.yml` for new example files; do not rename existing compose files unless explicitly requested
- MUST: For example files, add `.example` as a filename suffix
- MUST: Keep concrete compose files such as `compose.yml` and `docker-compose.yml` out of template repositories unless explicitly required
- MUST: Prefer checked-in example variants for compose files (for example `compose.yml.example` and `docker-compose.yml.example`)
- MUST: When creating compose examples, ensure real local compose filenames are included in ignore files
- MUST: Add `TODO.md` to baseline ignore files to keep local project task tracking out of version control unless explicitly requested
- MUST: Use UPPER_SNAKE_CASE for variable names by default unless language-specific standards override this
- MUST: Only deviate from variable naming rules when required by external interfaces, libraries, or framework constraints
- MUST: Name functions and methods using verbValue or verbObject patterns
- MUST: Prefer these verb prefixes for function and method names where applicable: add, change, check, create, generate, get, read, set
- MUST: Name variables explicitly to reflect type and scope (for example FILE_NAME, FILE_PATH, DIR_NAME, DIR_PATH)
- MUST: Use consistent abbreviations in variable names (for example DIR, CONF, VAR)
- MUST: Apply one variable naming convention per language as defined in language-specific standards, and do not mix conventions within the same file or function
- MUST: When a language-specific rule overrides a global rule, apply the override consistently across the entire change set
- MUST: If external-interface constraints require a different name, isolate and document that exception directly above the affected code
- SHOULD: Prefer local variables where practical in functions and methods

## Language-specific standards
- MUST: Language-specific rules override global naming rules when conflicts occur
- MUST: For Python, use snake_case for functions and methods unless external interfaces require otherwise
- MUST: For Python, use UPPER_SNAKE_CASE for variables, constants, and configuration identifiers
- MUST: For shell and Bash, use camelCase for function names following verbValue or verbObject patterns
- MUST: For shell and Bash, use UPPER_SNAKE_CASE for variables
- MUST: For shell and Bash, keep scripts POSIX-compliant unless Bash is explicitly required
- MUST: For JavaScript and TypeScript, use camelCase for functions, methods, and variables
- MUST: For JavaScript and TypeScript, use PascalCase for classes and components
- MUST: For JavaScript and TypeScript, use UPPER_SNAKE_CASE for constants
- MUST: For HTML and CSS, use BEM naming for CSS classes
- MUST: For HTML and CSS, keep semantic HTML structure and accessibility attributes by default
- MUST: For Swift (iOS, iPadOS, and Catalyst), follow Swift API Design Guidelines naming
- MUST: For Swift, prefer SwiftUI plus MVVM unless project constraints require UIKit
- MUST: For Swift, isolate platform-specific code behind adapters or extensions for Catalyst compatibility
- MUST: For Kotlin (Android), use Kotlin conventions for naming
- MUST: For Kotlin, prefer Jetpack Compose plus MVVM for new UI work
- MUST: For Kotlin, keep business and domain logic platform-agnostic where practical to ease iOS and Android parity
- MUST: For iOS and Android parity work, define shared feature specifications, naming, and state models before implementation
- MUST: For iOS and Android parity work, keep domain logic, API contracts, and validation rules aligned across platforms
- SHOULD: For iOS and Android parity work, mirror folder and module structure between iOS and Android apps where practical
