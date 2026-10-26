# P4B Advisory v2: typed findings, same human authority

Explicit opt-in: `p4b_advisory_protocol=p4b.advisory.2` plus existing advisory/P4B/P4A/enterprise opt-ins. Default remains `p4b.advisory.1`, default advisory disabled. No existing service settings are changed. A separately injected provider must declare its exact output protocol; protocol mismatch fails closed. This version is prepared offline only and has no paid acceptance.

`p4b.advisory.2` has the same top-level risk, recommendation, missing-evidence and human-review fields as v1, with a distinct finding DTO. v1 DTO/schema, old semantic protocols, historical reports and Golden remain unchanged. The optional enterprise report envelope stays `p4b.advisory.report.1`; added finding display fields are optional for consumers of old reports.

Each finding supplies `code`, `classification`, `resolution`, `policy_ref_ids`, `fact_refs`, and `rule_refs`. Optional `fact_key` identifies a required missing/conflicted fact. Semantic warnings require an exact employee `notes` quote plus a model `question`. Raw output is immutable; failed classifications never become INFO automatically.

| Code | Required class | Meaning / minimum support |
| --- | --- | --- |
| GENERAL_FINANCE_CHECK | INFO | Routine final invoice check; original invoice provenance, clauses 38/39 |
| VERIFIED_STAY | INFO | Stay and original invoice records exist; clause 39; no claim they match without review |
| POLICY_EFFECTIVE | INFO | Server-owned published/effective policy fact; clauses 53/55 |
| PRIOR_APPROVAL_VERIFIED | INFO | Prior business approval fact; clause 44; no financial/payment clearance |
| AMOUNT_WITHIN_LIMIT | INFO | Explicit HOTEL_LIMIT_MATRIX PASS rule ID; clause 15 or applicable original matrix row; no budget assertion |
| BUSINESS_PURPOSE_UNRESOLVED | WARNING | Exact candidate notes + unverified question + business clause |
| MISSING_REQUIRED_EVIDENCE | BLOCKER | Required fact in MISSING/DECLARED; own path + evidence obligation clause |
| RULE_FAILURE | BLOCKER | Explicit HOTEL_LIMIT_MATRIX FAIL rule ID + clause 15 |
| FACT_CONFLICT | BLOCKER | Required fact CONFLICTED or verified false + supporting obligation clause |

INFO uses `NOT_APPLICABLE` resolution; warning/blocker uses `UNRESOLVED`. Resolution of a concern is recorded by independent finance review or new evidence/reaudit, never by relabeling it. Unknown codes/classes/extra prose fields fail DTO. V2 deliberately has no semantic EXCEPTION_UNAUTHORIZED code: candidate notes cannot prove that an exception actually exists or requires authorization. Unsupported exception judgments stay UNDETERMINED/ESCALATE; old v1 exception and independently verified/manual exception execution gates remain unchanged. Each supplied policy ID must belong to the code's exact clause set; references to real unrelated clauses fail. Positive assertions require VERIFIED value, provenance, scope; declared candidate text cannot prove financial truth. Rule IDs must exist and outcomes must support the claim. The server verifies the whole source context before accepting any finding.

LOW may contain INFO. Unresolved warning/blocker cannot be LOW or recommend approval; blocker requires HIGH/UNDETERMINED. Required missing/false facts or non-PASS/absent rules prevent LOW and approval. Missing required facts must be reported. Recommendation of rejection requires its own validated BLOCKER and HIGH/UNDETERMINED risk; empty/INFO-only findings cannot justify rejection. Clause 18 (invoice obligations) is not an amount limit basis. Invalid finding material makes the effective recommendation/risk UNDETERMINED, and status FAILED/PARTIAL. No model error is silently corrected to earn a PASS.

Formal finding explanations are finite server presentations of accepted structures. Policy source text, fact paths and rule IDs are shown separately. Semantic model questions are marked unverified and cannot establish facts. The model's original JSON and transport response are preserved, including failures. No top-level free-text compliance explanation is accepted. The existing v1 attribution/P2 guards remain; a narrow budget-assertion rejection guard additionally catches the historical unsupported claim. These guards do not prove arbitrary Chinese text is truthful.

Every claim still requires authenticated finance manual review followed by explicit approval. An AI failure stays failed even after independent human clearance; it never converts to RULE_PASS or FINANCE_APPROVED. Existing version/digest, live authority/revocation, mandatory evidence, exception restrictions and first ERP write checks remain. Existing committed matching ERP effects can reconcile idempotently; uncommitted effects must revalidate. No changes to original databases, legacy endpoints or ERP implementation.

Limitations: this is a finite hotel-focused finding catalog, not general financial reasoning or a budget verification feature. Other supported expense kinds can use existing v1 or v2 with applicable findings; hotel-only codes fail on other kinds. Semantic questions need human verification; exact source quotes do not prove the concern is real. Local lexical DOCX retrieval and MockTransport evidence are not real BGE/DeepSeek acceptance. A new explicitly authorized one-shot retest is still required.
