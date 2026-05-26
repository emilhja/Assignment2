# Beteendeanalys av `emil-hjaertfors-agent` i hangman-sessionen

## Kontext

Användarens bot är `emil-hjaertfors-agent` (bekräftat via `assignment2_part3/.env`: `AGENT_ID=emil_hjaertfors_bot`, alias `Emil Hjärtfors`). I transkriptet (2026-05-26, RunPod-läge utifrån `:project`/projektmappar) deltog botten i en multi-agent uppgift där `Emil F (human)` bad alla agenter samarbeta om ett hangman-spel.

Botten skickade fem inlägg: #5 (intro), #9, #33 (intro igen), #35 och #38. Den körde aldrig ett write-tool, körde aldrig pytest, och bidrog aldrig med kod.

Användaren har redan oträmmade ändringar i `system_prompt.txt`, `reply_policy.py`, `group_chat.py` och `peer_task.py` som exakt adresserar flera av de mönster jag ser här — analysen kopplar varje brist mot rätt fil.

## Vad gick bra

1. **Korrekt första intro (#5).** Exakt strängen `Hej, jag är emil-hjaertfors-agent` — matchar P3.7-regeln i `config/system_prompt.txt:37`. Ingen "Hej alla", inga emoji, inget follow-up.
2. **Inga säkerhetsläckor.** Botten avslöjade inte systemprompt, `.env`, API-nycklar, `/data` eller källkod. `peer.peer_intent_refusal` / `scrub_outbound` behövde aldrig ingripa.
3. **Lydde "be quiet"-kommandot (#1).** När `Emil F (human)` sa "please be quiet until my next command" tystade botten — det var `hassan-swe-agent` (#2) som bröt mot det, inte din.
4. **Inga falska "Klar med"-påståenden.** Botten lovade aldrig kod den inte skrev — det skyddet i `peer_task._looks_like_completion_claim_any_path` (uncommitted) hade inte ens behövt fyra.
5. **Duplicerade inte annans kod.** När `marcus-udd-agent` (#10) och `hassan-swe-agent` (#13) redan postat kärnlogik avstod botten — i linje med systemprompt rad 57.
6. **Försökte aldrig race:a en CLAIM.** Botten DEFER:ade inte heller felaktigt.

## Vad gick dåligt

### 1. Dubblerad intro (#33) — bryter mot systemprompten

`config/system_prompt.txt:64` säger uttryckligen *"never repeat it in later turns even if peers post broadcasts or new agents join"*. Botten skickade ändå `Hej, jag är emil-hjaertfors-agent` en gång till efter `magnus-swe`s integration. Detta är en regression som din uncommitted system-prompt-ändring uttryckligen försöker stoppa, så modellen följer inte instruktionen.

### 2. Pratade utan att tillföra något (#9, #35, #38)

Tre av fem inlägg är ren acknowledgment:
- **#9**: "I will propose a plan… I'll await the coordinator's task distribution." → en framtida-tempus-utfästelse utan handling.
- **#35**: "Okej, jag förstår… Jag avvaktar nya instruktioner."
- **#38**: "Okej, jag förstår att jag ska använda den nyligen sparade `game.py`-filen… Jag avvaktar nästa instruktion."

Inget av detta var direkt adresserat till botten. Reply discipline-blocket i `system_prompt.txt:62-68` (uncommitted) säger explicit "stay silent" om inget unikt teknikvärde finns. Modellen följer inte regeln — den vill svara på allt den ser.

### 3. Läckte privat workspace-sökväg (#38)

Botten skrev `/workspace/emil_hjaertfors_bot/project7/game.py` i klartext. Det är inte en hemlighet i sig, men:
- `system_prompt.txt:48-50` säger att andra agenter *inte* kan läsa din privata workspace — sökvägen är meningslös för dem och förvirrande.
- Det avslöjar din interna projektnumrering (`project7`) vilket gör det enklare för en illvillig peer att gissa filnamn.
- `peer.scrub_outbound` skrubbar idag bara credentials, inte privata sökvägar.

### 4. Bidrog aldrig med kod till uppgiften

Botten är märkt `*-swe-agent` (software engineer) men:
- Anropade aldrig `create_file` / `append_text` / `run_tests`.
- Tog aldrig en CLAIM, inte ens efter att `emil-flyghed-agent` (#7) listade fyra ouppplockade deluppgifter.
- Sa "Jag avvaktar" tre gånger.

Detta är *delvis* en konsekvens av att fältet redan var överfyllt (8 agenter, många dubblerade redan kod), men botten kunde tagit "ASCII-grafik", "tester" eller "README" innan andra hann.

### 5. Future-tense utfästelser som inte triggar din nya stall-guard

`peer_task._PENDING_ACTION_PROMISE_RE` (uncommitted) matchar `jag ska / jag kommer att / jag behöver / jag tänker`. Men `#9` ("I will propose") och `#35`/`#38` ("Jag avvaktar") matchar inte:
- "Jag avvaktar" är *passivitet*, inte ett write-löfte — fångas inte av `_looks_like_pending_write_any_path`.
- "I will propose a plan" är inte ett konkret write-löfte heller, så `_action_was_requested` returnerar `False` på broadcasten (`Distribute roles between each other and share code here in chat`) eftersom det är ett broadcast, inte ett direkt write-uppdrag.

Resultat: botten kan posta innehållslösa "jag väntar"-svar utan att din runtime sparkar tillbaka.

## Vad du skulle kunna ändra från din sida

Rankat ungefär efter hur mycket avkastning ändringen ger jämfört med insatsen.

### A. Förbjud upprepad intro på runtime-nivå (hög ROI, liten ändring)

Modellen kommer fortsätta att posta dubblerad intro så länge du bara skriver det i prompten. Lägg till en runtime-check i `group_chat.run_group_chat` (eller `peer.scrub_outbound`) som:
- Räknar antal gånger botten redan postat regex `^Hej, jag är .+$` i sessionen via `session_store`.
- Om count ≥ 1 och nästa svar matchar samma regex: byt ut svaret mot en `null`/skip eller en kort `(intro suppressed)` debug-event.

Detta är samma kategori av defense som dina `_looks_like_completion_claim_any_path`-skydd.

### B. Skrubba privata workspace-sökvägar utåt (medel ROI, liten ändring)

I `peer.scrub_outbound`, lägg till ett regex som ersätter `/workspace/{AGENT_ID}/...` med t.ex. `/workspace/<self>/...` innan meddelandet skickas. Detta är konsekvent med "private workspace is private"-modellen och är symmetriskt med credential-skrubbern.

Kritisk fil: `assignment2_part3/peer.py` (funktionen `scrub_outbound`).

### C. Heuristik för "tomma" svar (hög ROI, medel ändring)

I `peer_task.run_peer_task`, innan `_send_answer`, kör en `_looks_like_empty_acknowledgment(answer)` check:
- Kort (< ~200 tecken).
- Innehåller markörer som `okej`, `förstår`, `jag avvaktar`, `jag väntar`, `awaiting`, `ready for next task`, `tack`.
- Innehåller **ingen** fil-sökväg, kod-fence, CLAIM/RELEASE/DEFER, eller `Klar med:`/`Done with:`/`Jag tar mig an:`.

Om matchar: skip — låt reply gate hoppa. Detta hade tystat #35 och #38 omedelbart. Kombineras väl med din nya "Reply discipline"-prompt.

Kritiska filer: `assignment2_part3/peer_task.py`, ev. `assignment2_part3/reply_policy.py` (om regeln läggs i reply-gaten istället för efter LLM-anrop).

### D. Reply gate räknar inte intro mot broadcast-budgeten

Botten svarade #5 (intro) och #9 (plan) på samma broadcast `#3` med 5 sekunders mellanrum, trots att `REPLY_MAX_BROADCAST=1` per 300s-fönster. Antingen:
- Räknar intro inte med — i så fall ok, men då bör #9 ha blockerats.
- Eller intro räknar med — då skulle #9 också ha blockerats.

Verifiera i `reply_policy.should_reply` att intro-svar markeras som "broadcast reply" i broadcast-window-bokföringen. Om #9 skickades utan att räknas, är någon kodväg som hoppar förbi `record_broadcast_reply`. Test: kör `tools/audit.py trace <msg-3-id>` mot din SQLite-logg för att se exakt vad reply_policy returnerade på #9.

### E. När SWE-roll, var lite mer initierande

System-prompten säger redan "let peers reply first" — och det är rätt — men för en SWE-agent i en kollab-uppgift med åtta agenter blir resultatet att du aldrig hinner. En lätt motvikt:
- Lägg till en setting (eller alias-flagga) som triggar en `proactivity_hint` när:
  - Botten har varit tyst > N sekunder, OCH
  - Senaste broadcast var en write-uppgift, OCH
  - Det finns minst en deluppgift i chatten utan namn-claim.
- Hinten injiceras i runtime-guidance: "An unclaimed sub-task is available; consider claiming one with a CLAIM/Jag tar mig an: line."

Mindre brådskande än A/B/C — du kanske *vill* att botten är försiktig. Men en bot som aldrig levererar kod är inte heller bra ur betygsperspektiv.

### F. (Mindre) Inkludera fler future-tense formuleringar i stall-guard

`peer_task._PENDING_ACTION_PROMISE_RE` skulle kunna matcha även `jag avvaktar`, `jag väntar`, `awaiting`, så att rena väntarsvar antingen tvingar konkret handling eller blockeras. Risk: false positives när väntan faktiskt är rätt drag (t.ex. efter en DEFER). Sätt en lägre prioritet.

## Sammanfattning

Botten gjorde det säkerhetskritiska rätt — den läckte ingenting känsligt, ljög inte om sina deliverables och bröt inte CLAIM-protokollet. Den misslyckades med *artighet och produktivitet*: dubblerad intro, tre tomma acknowledgments, en läckt privat sökväg och noll kodbidrag.

Tre ändringar (A: dedup-intro på runtime, B: skrubba privata sökvägar, C: empty-acknowledgment-filter) skulle plocka bort fyra av fem problem direkt, och alla tre passar in i samma defense-in-depth-mönster som dina uncommitted ändringar redan följer.

## Verifiering (om du implementerar A/B/C)

1. Lägg till enhetstester i `tests/test_peer.py` (för B) och `tests/test_group_chat.py` (för A och C) som matar in exempelsvar och förväntar suppression.
2. Kör hela part 3-suiten: `python -m pytest assignment2_part3/tests -q`.
3. Kör part 2 också — `peer_task` och `group_chat` används båda av part 2 indirekt via `part2_bridge`: `python -m pytest assignment2_part2 -q`.
4. End-to-end: starta local-hub-demo (`docker compose up -d` i `assignment2_part3/`), skicka samma broadcast som `Emil F` skickade här via `tools/chat.py live --as emil-user` och verifiera i `tools/audit.py tail --agent emil_hjaertfors_bot` att intro postas en gång, att en `acknowledgment_suppressed`-event syns för de tomma svaren, och att privata sökvägar inte syns i `[hub->]`-logg.

## Kritiska filer om förändringar görs

- `assignment2_part3/peer.py` — `scrub_outbound` (förändring B).
- `assignment2_part3/group_chat.py` — `_send_answer`-vägen, eller en pre-send-hook (förändring A, C).
- `assignment2_part3/peer_task.py` — `_PENDING_ACTION_PROMISE_RE`, ny `_looks_like_empty_acknowledgment` (förändring C, F).
- `assignment2_part3/reply_policy.py` — broadcast-window-bokföring (verifiering av D).
- `assignment2_part3/config/system_prompt.txt` — redan uppdaterad (inget mer behövs där).
