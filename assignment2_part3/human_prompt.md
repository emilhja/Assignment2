  Better Human Prompt

  Instead of “all agents, create…”, use:

  All agents: project directory is /workspace/project10.

  Implement a simple Python calculator with add, subtract, multiply, divide. No UI.

  Roles:
  - josef-agent: implement calculator.py
  - emil-hjaertfors-agent: write and run pytest tests
  - emil-flyghed-agent: review final files after tests pass
  - lullo-swe-agent: only summarize final state, no repeated delegation proposals

  Do not report done without tool-confirmed file paths and test output.

  Answer to Emil’s Last Question

  The best answer from the agents should have been