rule: debugging-discipline

# Debugging and type-fixing discipline

Seven rules, each from a failure that cost real time. When you debug, fix a type error or a warning, or write a check, a gate or a stop condition, read the `debugging-discipline` skill first: it has each rule in full and the incident behind it.

1. Read the dependency's own types before searching the web
2. A partial win does not reset "two tries, then change tracks"
3. State the code fact that makes a diagnostic true before fixing it
4. Make the check a gate, not a side quest
5. Exit code 0 is not proof: assert on the output
6. Warnings are defects, and static types are mandatory
7. A stop or budget guard must be seen to trip, and must fail closed
