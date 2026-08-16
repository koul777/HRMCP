# Current Purpose

This project implements NCS-based HR training recommendation.

NCS is treated as a structured job-competency source:

- competency units describe work capability;
- competency elements group execution units;
- performance criteria are task nodes;
- KSA items describe task performance requirements;
- training courses are linked to units and KSA concepts.

The MCP should answer training questions through the path:

```text
Task query -> NCS task -> KSA concepts -> training courses -> evidence
```

SQF is not used in the active recommendation path.
