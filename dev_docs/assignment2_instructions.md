# Assignment 2 – AI Agent från Scratch

## Översikt

Assignment 2 handlar om att bygga en egen AI-agent i Python. Uppgiften är uppdelad i tre separata delar där agenten utvecklas stegvis:

1. **Del 1:** Enkel ReAct-agent med bash-kommandon via hemmagjord function-calling.
2. **Del 2:** Starkare agent med structured output, säkrare tool-calling och filredigering.
3. **Del 3:** Multi-agent-samarbete via gemensam group chat.

Syftet är att förstå hur agentbaserade system fungerar från grunden, utan att gömma bort kärnlogiken bakom färdiga agentramverk.

---

## Viktiga grundregler

Agenten ska byggas med egen Python-kod och API-anrop till exempelvis:

- OpenAI Platform API
- Anthropic API
- OpenRouter
- Groq
- lokala modeller
- liknande LLM-tjänster

Följande får inte användas som del av agentens funktion:

- OpenCode
- KiloCode
- Claude Code
- Cursor
- Anti-gravity
- Codex
- färdiga AI-IDE-agentlösningar

Ramverk som LangGraph, LangChain och LlamaIndex får endast användas i de delar där det uttryckligen tillåts.

---

## Deadline och examination

- Assignment 2 är live från: **14 maj 2026**
- Deadline: **28 maj 2026 kl. 22:00**
- Uppgiften ska lämnas in i **tre separata inlämningar**, en per del.
- Fredag **29 maj 2026** kopplas alla Del 3-agenter ihop på lektion.
- En **GraderBot** kommer delta och utvärdera om agenten fungerar enligt kriterierna.
- Närvaro vid agentmötet är obligatorisk eftersom det är en del av examinationen.

---

# Del 1 – Minimal ReAct-agent

## Mål

Bygg en enkel ReAct-agent som kan använda bash-kommandon via hemmagjord function-calling.

Agenten ska:

- vara byggd i Python
- använda rå text-output från modellen
- själv detektera tool-calls via egen stränghantering
- kunna köra bash-kommandon via egen Python-funktion
- inte använda färdiga agentramverk
- inte använda inbyggd function-calling
- inte använda structured outputs
- inte använda JSON tool-calling

---

## ReAct-format

Agenten ska använda ett enkelt textbaserat protokoll.

Exempel på tool-call:

```text
Thought: Jag behöver se vilka filer som finns.
Action: bash
Command: ls -la