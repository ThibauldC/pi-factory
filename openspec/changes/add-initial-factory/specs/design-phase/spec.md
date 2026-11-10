# Spec Delta

## Purpose

Produces a reviewable design for an issue before any code is written, so a human can catch a wrong direction early, when it is cheap to fix.

## ADDED Requirements

### Requirement: Read-only design session
The design phase SHALL run pi in the issue's worktree with the built-in tools limited to `read`, `grep`, `find` and `ls`. The factory SHALL reject a design round that leaves uncommitted changes in the worktree.

#### Scenario: Design does not modify the repo
- **WHEN** the design session finishes
- **THEN** `git status --porcelain` in the worktree is empty

#### Scenario: Worktree modified during design
- **WHEN** the worktree has uncommitted changes after the design session
- **THEN** the factory discards them, moves the issue to Blocked, and comments that the design round modified files

### Requirement: Design prompt inputs
The design prompt SHALL include the issue identifier, title, description and all human comments. For a redesign round, it SHALL also include the previous design and mark which comments were posted after it.

#### Scenario: First design
- **WHEN** an issue with no previous design enters the design phase
- **THEN** the prompt contains the issue's title, description and existing human comments

#### Scenario: Redesign
- **WHEN** an issue with a previous design comment re-enters the design phase
- **THEN** the prompt contains the previous design and the human comments posted after it, marked as feedback to address

### Requirement: Design comment format
The design SHALL be posted as one Linear comment, starting with a heading that names the round (for example `Design (round 2)`). It SHALL contain these sections: Approach, Files likely touched, Acceptance checks, and Open questions. Each open question SHALL be numbered and give a recommended answer.

#### Scenario: Well-formed design
- **WHEN** the design session returns text containing all required sections
- **THEN** the factory posts it as a comment and moves the issue to Design Review

#### Scenario: Malformed design
- **WHEN** the session's final text is missing a required section
- **THEN** the factory asks the same session once to correct the format. If it is still malformed, it moves the issue to Blocked with the raw output attached as a comment

### Requirement: Design is the build contract
The factory SHALL record the comment ID of the most recent posted design for each issue. The build phase SHALL use that design, plus the human comments posted after it, as its specification.

#### Scenario: Owner answers open questions
- **WHEN** the owner replies to the design's open questions in a comment and then moves the issue to Ready for Build
- **THEN** the build prompt includes both the design and that reply
