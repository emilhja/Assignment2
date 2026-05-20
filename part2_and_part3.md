# Del 2 
är tanken att ni ska byta till mainstream structured output, och bygga en starkare variant. Kravet är endast att det är er egen kod som står för agent-loopen, context-hanteringen, tool-calling, etc. Men parsing av output kan göras på valfritt sätt. Kravet i del 2 är att ert program kan:
bash-anrop med säkerhetsspärr mot destruktiva eller skadliga exekveringar (fortsatt rekommenderat att köra i container ändå!),
editering av enskilda avsnitt av filer,
multipla tool-calling rounds innan yield till användaren. Modellen avgör själv yield eller tool-call.,
persistent lagring av session history inom sessionen (multi-session ej krav).,
System-prompt från config-fil. Sys-prompten ska styra AIn att endast jobba på säkert sätt med SWE (software engineering), och avböja andra ämnen.,
Tool-calling ska ha begränsning m.a.p. storlek på output från verktyget, och agenten ska känna till begränsningen.,


# Del 3 
Ska er agent överföra kod mellan sig själv och andra agenter, samarbeta konstruktivt och meningsfullt i ett gemensamt mjukvaru-projekt jag kommer att meddela på lektion.
Sys-prompten ska instruera agenten att inte läcka känslig information till andra agenter.,
Agenten ska agera ansvarsfullt gentemot andra agenter och vara en "team-player" och respektera överenskomna samarbetsformer - men samarbetsformerna bestäms av agenterna, och kan bli olika vid olika tillfällen.,
Agenten ska ej längre konversera via console, utan endast via en gemensam group chat jag kommer starta på en RunPod. Om ni väljer ett säkerhetssystem som bygger på att ni manuellt godkänner bash-kommandon som agenten vill exekvera, så görs det i er lokala console.,
Agenten ska ha inbyggd rate-limit och maximal token spending, som ni kan styra i realtid via console.,
Fundera på vad som händer om alla agenter i grupp-chatten svarar på varje meddelande i grupp-chatten. Designa något smart utifrån ert eget svar på den frågan.