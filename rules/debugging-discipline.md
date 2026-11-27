rule: debugging-discipline

# Debugging and type-fixing discipline

When you debug, fix a type error or warning, or write a check, gate or stop condition, read the `debugging-discipline` skill first: each rule below in full, with the failure behind it.

1. Read the dependency's own types before searching the web
2. A partial win doesn't reset "two tries, then change tracks"
3. State the code fact that makes a diagnostic true before fixing it
4. Make the check a gate, not a side quest
5. Exit code 0 is not proof: assert on the output
6. Warnings are defects; static types are mandatory
7. A stop or budget guard must be seen to trip, and fail closed
