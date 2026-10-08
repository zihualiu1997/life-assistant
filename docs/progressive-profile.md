# Five-day onboarding and long-term profile

The assistant can learn about a new user gradually. There is no mandatory signup questionnaire, and completing five days does not mean every field must be filled.

| Day | First optional question | Later, if useful |
| --- | --- | --- |
| 1 | Preferred name | Conversation and assistance preferences |
| 2 | A typical day | Usual energy patterns |
| 3 | One current priority | What useful progress would look like |
| 4 | A habit or method that helps | Available time and resources |
| 5 | Preferred support | Topics or information the user does not want recorded |

Each contact asks one question. At most two onboarding contacts run per day, at least four hours apart, inside the existing schedule and total contact limits. Event follow-ups take priority. Known, previously asked, skipped, deferred, or declined questions are not repeated. Two unanswered onboarding contacts suspend discovery until another real interaction. Missed questions are not sent in a catch-up batch after day five.

## Consent and controls

In the managed pilot, cloud processing and automatic memory consent must already permit the conversation tools; proactive-contact consent independently controls scheduled delivery. The onboarding/profile feature adds its own opt-in and never overrides those settings.

The assistant explains local storage and available controls before inviting the user to say `开始认识我`. Users can continue normal use without starting onboarding. Existing profile documents are not automatically re-enrolled.

Supported controls:

- `开始认识我`: begin the five-day discovery window and enable profile updates.
- `暂停认识我` / `继续认识我`: pause or resume, shifting the remaining stage dates.
- `结束引导`: stop discovery while preserving the profile-update preference.
- `跳过这个问题` / `这题以后再说` / `这题不想记录`: skip, defer, or decline the most recent question.
- `关闭资料更新` / `开启资料更新`: disable or enable subsequent profile updates.
- `不要记录财务` / `允许记录财务`: decline or re-enable one field. Health, relationships, family, work, exercise, food, learning, energy, and routine have equivalent Chinese commands.
- `只聊不记`: exclude this message from capture.

Health, finances, and relationships are not proactively interviewed. Record only relevant information the user volunteers. A disabled field stays disabled when global profile updates are re-enabled. These controls do not claim to delete existing chat history or independent diary records.

## Storage and evidence

`life_profile_manage` writes only the fixed profile and monthly change records:

- `02_生活领域/10_自我认识/个人现状.md`: current sourced statements and pending conflicts, with the existing document preserved below the managed section.
- `02_生活领域/10_自我认识/成长记录/YYYY-MM.md`: original statements, source IDs, observation dates, corrections, and meaningful events.
- `.local/profile/`: private state, recovery transaction, and backups. Never commit this directory.

Diary, training, project, and domain details keep their existing primary locations. The original questionnaire remains historical evidence. Do not copy one user's completed profile into another user's instance. The older `02_生活领域/个人资料.md` report opt-in document remains separate; new profile contents are not automatically added to report context.

Updates require a current owner-bound message, its exact quote, and an exact quoted value. Goals, events, historical information, and current statements have distinct kinds. A different value becomes a pending conflict unless an explicit correction identifies the current revision. Unknown dates remain unknown. Unconfirmed voice transcripts, obvious hypotheticals, questions, third-party statements, credentials, and declined messages are rejected.

Writes share the restricted knowledge lock, preserve backups, check revisions, and recover prepared multi-file operations without overwriting concurrent changes. Only a successful receipt supports a claim that the record was saved. The conversation model still classifies meaning; provenance allows correction if it selects the wrong field.

The profile tool exposes time-to-review hints without sending additional scheduled questions: 14 days for health limitations, 365 days for identity/preferences/boundaries, and 90 days for other current fields. Age, health, and historical context are not silently updated by inference.

## Integration and validation

The existing container gateway config registers `life_profile_manage`, `life_knowledge_manage`, and `life_preferences` alongside check-in/follow-up and knowledge-search tools. Conversation hooks do not permit arbitrary shell, code, runtime configuration, or agent delegation actions. No registration platform, external profile service, or additional scheduler is introduced.

Source is in `server/life_proactive/onboarding.py`; the bridge is in `proactive-bridge/`. Tests cover all five days, existing users, no replies, controls, consent, conflicts, source identity, interrupted writes, manual edits, and month rollover. Local simulated tests and tool registration do not establish real-device five-day acceptance.
