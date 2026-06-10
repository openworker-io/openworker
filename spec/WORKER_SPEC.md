# OpenWorker Worker Spec — Schema Reference v0.1

## Overview

A Worker Spec is a YAML file that fully defines a digital worker
employed inside an organization. It combines four things that
previously had no standard format:

  1. Job description     → role, skills, knowledge
  2. Permission manifest → allowed / approval_required / blocked tools
  3. HR record           → identity, org placement, autonomy level
  4. Runtime config      → model, execution backend, audit settings

One file. Version-controlled in Git. Readable by a CEO.
Executable by the OpenWorker runtime.

---

## File naming convention

  worker.<name>.yaml
  worker.maya.yaml
  worker.codera.yaml
  worker.sara-hr.yaml

---

## Top-level fields

| Field          | Required | Description                              |
|----------------|----------|------------------------------------------|
| apiVersion     | yes      | always `openworker/v1`                   |
| kind           | yes      | always `Worker`                          |
| identity       | yes      | who this worker is                       |
| org            | yes      | where they sit in the org chart          |
| role           | yes      | what they do                             |
| autonomy       | yes      | how much they can do independently       |
| base_skills    | yes      | loaded automatically (do not remove)     |
| role_skills    | yes      | capabilities specific to their role      |
| tools          | yes      | permission tiers for every tool          |
| knowledge      | no       | company docs and tone profile            |
| behavior       | no       | working hours, style, rate limits        |
| approvals      | yes      | approval routing and notification config |
| performance    | no       | trust score tracking                     |
| audit          | yes      | logging and compliance settings          |
| runtime        | yes      | model and execution backend              |

---

## Autonomy levels

| Level | Label   | Behavior                                          |
|-------|---------|---------------------------------------------------|
| L1    | Intern  | Can observe and suggest. Cannot execute anything. |
| L2    | Junior  | Can execute. All actions require approval.        |
| L3    | Mid     | Executes low-risk freely. High-risk needs approval|
| L4    | Senior  | Executes most tasks. Escalates exceptions only.   |
| L5    | Lead    | Can coordinate and assign other workers.          |

Autonomy level is not set manually after onboarding.
It is promoted by the manager based on trust_score reaching
the configured promotion_threshold.

---

## Tool permission tiers

### allowed
Worker can use this tool freely without any human sign-off.
Use for: read-only tools, drafting tools, internal tools with
low blast radius.

### approval_required
Worker must submit an approval request before execution.
Approval is routed to reports_to via the configured channel.
If no response within sla_hours, routes to backup_approver.
auto_approve_after_sla must always be false for external actions.

### blocked
Hard stop. The runtime will refuse to execute this tool regardless
of any instruction — from the worker itself, from another agent,
or from any prompt injection attempt. Blocked tools cannot be
unlocked via conversation. Only a manager editing the spec file
and re-deploying can change a blocked tool.

---

## Trust score

The trust_score (0–100) is computed by the runtime after every task:

  trust_score =
    (approval_rate × 0.50) +
    ((1 - rejection_rate) × 0.30) +
    (appropriate_escalation_rate × 0.20)

Where:
  approval_rate             = actions approved without changes / total actions
  rejection_rate            = actions rejected by manager / total actions
  appropriate_escalation    = escalations manager confirmed were correct

Trust score is surfaced in the Manager Dashboard.
When trust_score >= promotion_threshold, the manager receives
a promotion recommendation notification.

---

## Base skill pack (loaded for every worker)

These cannot be removed. They are the minimum viable capability
set for any worker to function inside an organization:

  email             read, draft, send (send always requires approval at L2)
  calendar          view schedule, create events (external events need approval)
  messaging         Teams and Slack — read and post in assigned channels
  video_calls       join meetings, produce summaries
  document_reader   read and summarize any doc, PDF, spreadsheet
  web_search        search and browse for research
  policy_rag        query the company's knowledge base
  task_logger       auto-log every action (cannot be disabled)
  escalation        detect when to pause and ask the manager

---

## Role packs (built-in)

Role packs add domain-specific skills on top of base:

  marketer     linkedin, email campaigns, image gen, SEO, reporting
  developer    github, code read/write, tests, PRs, CI monitoring
  recruiter    ATS, candidate sourcing, resume screen, interview scheduling
  sales        CRM, lead research, outreach drafting, pipeline updates
  tester       test case writing, test execution, defect logging, coverage
  hr_assistant employee records read, onboarding workflows, policy Q&A
  scrum_master jira read/write, sprint management, standup summarization

Custom role packs can be defined in the repo under /packs/<name>.yaml

---

## Blocked tools — non-negotiable defaults

Every worker spec ships with these blocked by default.
They cannot be in allowed or approval_required:

  payroll_systems
  hr_systems (except hr_assistant role — limited read only)
  production_databases
  financial_erp
  customer_pii_export
  code_repositories (except developer role)
  infrastructure_tools
  legal_document_signing

---

## Minimal valid spec (10 lines)

  apiVersion: openworker/v1
  kind: Worker
  identity:
    name: Alex
  org:
    reports_to:
      email: manager@company.com
  role:
    title: General Assistant
    pack: general
  autonomy:
    level: L2
  tools:
    blocked:
      - payroll_systems
      - hr_systems
      - production_databases
  approvals:
    channel: slack
  audit:
    log_all_actions: true
  runtime:
    model: claude-sonnet-4-6

Omitted sections use defaults defined in openworker-defaults.yaml

---

## Versioning

Spec files should be committed to Git alongside your codebase.
Treat them as infrastructure-as-code. Every change to a worker's
permissions is a PR — reviewable, auditable, reversible.

Recommended repo structure:

  /workers
    worker.maya.yaml
    worker.codera.yaml
    worker.sara-hr.yaml
  /packs
    marketer.yaml
    developer.yaml
    custom-finance.yaml
  openworker-defaults.yaml
  openworker.config.yaml

---

## Changelog

v0.1.0  2025-06-03  Initial spec. Maya (marketer) reference implementation.
