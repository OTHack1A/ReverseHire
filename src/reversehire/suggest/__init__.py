"""Company suggestions from the user's own assistant CLI, verified by the app.

Why: the map-based discovery misses companies whose map entry is missing or badly
tagged (a well-known ICT/network integrator of the owner's province was not found).
The assistant is asked right away for the companies with affinity (first 10, then
"Expand" for the next ones), and every suggestion is checked by the app before it can
become a company card.

Flow (all on worker threads; nothing raises into the caller)::

    from reversehire import suggest

    brief = suggest.build_profile_brief(cv_profile, affinity_preferences,
                                        suggest.SearchArea(kind="province", province="FC",
                                                           province_name="Forlì-Cesena"))
    show(brief.preview_text())                      # exactly what will be sent
    batch = suggest.suggest_companies(brief, cli=clis["claude"], n=10, exclude=known_names,
                                      language="it", cancel=event, timeout=240,
                                      allow_web_search=settings.suggest_allow_web_search)
    results = suggest.verify_batch(batch.suggestions, fetcher=fetcher, gateway=gateway,
                                   registry=registry_client, place_index=osm_index,
                                   area=brief.area, cli=batch.cli,
                                   max_total_requests=BATCH_REQUESTS, max_requests_each=REQUESTS_EACH)
    for r in results:
        seed = r.to_company_seed()                  # None unless VERIFIED / PARTIAL
    # "Expand": suggest_companies(..., offset_token=batch.offset_token)

Decision on web search (default OFF, opt-in setting ``suggest_allow_web_search``):

* Default: NO tools at all — the assistant answers from its own knowledge; the app
  then verifies every claim (site, identity, place) from named sources. This is the
  cheapest call on the user's plan, the fastest, and it exposes the assistant to no
  untrusted web text (no prompt-injection route).
* Web search does not make the answer trustworthy: a model that "found" a page is not
  proof, so verification is needed either way. It can help for small local companies
  the model barely knows, so it is offered as an opt-in.
* Opt-in rules: Claude Code gets ``--tools WebSearch`` (the built-in search tool that
  runs on Anthropic's side and returns search results; WebFetch, file tools, Bash and
  every other tool stay off, --restricted and the generated empty MCP config stay);
  Codex gets ``web_search='live'`` with the read-only sandbox, approval "never" and the
  shell/browser/plugin features still off. Nothing that can read or write local files
  or run commands is ever enabled; any other tool call stops the run.

Cost honesty: each call uses the user's own assistant plan. `SuggestionBatch.usage`
holds what the CLI reported (tokens, cost estimate, turns, web searches) and
`SuggestionBatch.notices` the per-call notice texts.
"""

from .brief import (AREA_COUNTRY, AREA_PROVINCE, AREA_RADIUS, AREA_REGION, ProfileBrief, SearchArea,
                    build_profile_brief, find_leaks)
from .engine import (ERR_CANCELLED, ERR_CLI, ERR_EMPTY, ERR_INTERNAL, ERR_MALFORMED, ERR_NO_CLI,
                     SuggestCallbacks, SuggestionBatch, suggest_companies, usage_notices)
from .prompt import COMPANY_TYPES, Suggestion, build_prompt, parse_answer
from .runner import Usage, build_suggest_claude_command, build_suggest_codex_command
from .texts import FALLBACK as TEXTS_EN
from .texts import text
from .verify import (PARTIAL, REJECTED, STATUSES, UNVERIFIED, VERIFIED, BatchCallbacks, VerifiedSuggestion,
                     area_contains, check_site, verify_batch, verify_suggestion)

# Verification budget used by the GUI: 8 requests per company, about 80 per batch of 10.
REQUESTS_EACH = 8
BATCH_REQUESTS = 80

__all__ = [
    "BATCH_REQUESTS", "REQUESTS_EACH", "AREA_COUNTRY", "AREA_PROVINCE", "AREA_RADIUS", "AREA_REGION", "BatchCallbacks", "COMPANY_TYPES",
    "ERR_CANCELLED", "ERR_CLI", "ERR_EMPTY", "ERR_INTERNAL", "ERR_MALFORMED", "ERR_NO_CLI", "PARTIAL",
    "ProfileBrief", "REJECTED", "STATUSES", "SearchArea", "SuggestCallbacks", "Suggestion", "SuggestionBatch",
    "TEXTS_EN", "UNVERIFIED", "Usage", "VERIFIED", "VerifiedSuggestion", "area_contains", "build_profile_brief",
    "build_prompt", "build_suggest_claude_command", "build_suggest_codex_command", "check_site", "find_leaks",
    "parse_answer", "suggest_companies", "text", "usage_notices", "verify_batch", "verify_suggestion",
]
