# Java Rules

How TigerStyle and NASA's Power of 10 map onto Java, with Android apps as the main
target. Read this instead of `python-rules.md` when the target is Java. Checked on
2026-11-08 against Temurin 17.0.21 (javac, java), Gradle 8.14.5, a real AGP 8.4.2
project (compileSdk 35, minSdk 26, R8 on in debug), D8 9.2.4-dev from build-tools
37.0.0, an API 37 arm64 emulator (`dalvikvm`), and lint-checks 31.4.2.

No TigerStyle port for Java was found (searched 2026-11-08), so this file maps the
upstream principles itself. Google's Java style guide covers formatting only.

Each rule follows the rule contract in SKILL.md: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (SKILL.md, "When to apply"): the full set is for code where a
bug corrupts data or state. For glue and small tools, keep the checks, bounds and
gates, and relax the allocation rules.

## 0. Project setup (copy this first)

```groovy
// app/build.gradle
android {
    lintOptions {                            // `lint {}` in newer AGP; same keys
        baseline file("lint-baseline.xml")   // only issues not in the file fail
        abortOnError true                    // the default; say it so nobody flips it
        checkReleaseBuilds true
        xmlReport true
    }
    testOptions {
        unitTests.all { it.timeout = java.time.Duration.ofMinutes(10) }   // a hung test fails
    }
}

tasks.withType(JavaCompile).configureEach {
    options.compilerArgs += ['-Xlint:all', '-Werror']
}
```

- `-Xlint:all -Werror` *verified* on the plain Gradle `java` plugin (Gradle 8.14.5):
  one raw type fails the build. On an AGP project it is **unconfirmed**; on an old
  codebase start with `-Xlint:all` alone and gate the count by message (section 16).
- AGP 8.4.2 lint needs JDK 17: *verified* it crashes on JBR 25 and runs on Temurin 17.
- `Test.timeout` is a Gradle task property; wiring it through `unitTests.all` on AGP
  is **unconfirmed**.

## 1. Always-on checks, not `assert` — Power of 10 rule 5

> "The code's assertion density should average to minimally two assertions per
> function. … Assertions must be side-effect free and should be defined as
> Boolean tests." — Holzmann

- **Default:** an explicit check that throws, at least two per function:
  ```java
  if (count == RING_FRAMES) {
      throw new IllegalStateException(
              "Ring.push: count " + count + " == RING_FRAMES " + RING_FRAMES);
  }
  ```
  Bad argument → `IllegalArgumentException`, bad object state →
  `IllegalStateException`, null → `Objects.requireNonNull(x, "Ring.push: gap")`.
  Guava's `checkArgument`/`checkState`/`checkNotNull` throw the same three
  (*cited*, Guava wiki) if Guava is already a dependency; don't add it for this.
- **Banned:** `assert` as the only guard of an invariant. Side effects inside any
  check (CERT EXP06-J).
- **Why:** one `assert` behaves three ways in one Android project:
  - JVM unit tests: ON. Gradle's `Test` task defaults `enableAssertions=true`
    (*verified*: `Ring.assertsOn()` true under `gradle test`; plain `java` prints
    "assert OFF" until `-ea`).
  - Debug APK: ON, forced at compile time. *Verified* in the project's own R8 debug
    dex (`dexdump` of `minifyDebugWithR8/classes.dex`): no `$assertionsDisabled`
    field, and xz's `assert` in `LZEncoder.setPresetDict` throws `AssertionError`
    with no flag check. The AGP 4.1 notes say so (*cited*).
  - Release APK: gone. D8's default strips them: `--release`, then `dalvikvm -ea`
    on the emulator prints "assert OFF" (*verified*). That AGP release builds
    actually take this default is *cited* (R8 commit, AGP 4.1 notes), not checked.
  - ART ignores `-ea`: D8 `--force-passthrough-assertions` plus `dalvikvm -ea` still
    prints OFF (*verified*); ART's own test expects "didn't assert (is '-ea'
    implemented?)" (*cited*).
  So `assert` passes in tests, may crash a debug user, and vanishes in release.
- **Exception:** `assert` for a check too expensive to ship (an O(n) scan in a hot
  path), with a comment saying release drops it. Never on a public method's
  arguments (CERT MET01-J). Effective Java 3rd ed. Item 49 allows `assert` for
  non-public parameters; on Android that is the trap above, so this file overrides it.
- **Proof:** count the throwing checks per function; each has a test (section 14).
- **Enforced by:** review. No lint flags `assert` on Android.

## 2. Invariant exceptions vs operating errors

- **Default:** two kinds, kept apart.
  - *Invariant* (the code is wrong): unchecked `IllegalStateException` /
    `IllegalArgumentException` / NPE. Never caught except at the sites in rule 4.
  - *Operating error* (the world is wrong: file missing, parse of untrusted input,
    socket closed): a checked exception or a result value, handled where it can be,
    and rethrown with its cause: `throw new IOException("Config.load: " + path, e)`.
- **Banned:** an invariant check on untrusted input (bad input is expected, not a
  bug); `new RuntimeException(msg)` that drops the cause; `e.getMessage()` copied
  into a new exception instead of chaining `e`.
- **Why:** TigerStyle: "The only correct way to handle corrupt code is to crash."
  Holzmann's rule 5 wants a recovery action instead. Split the two and both hold:
  crash on bugs, recover from the world.
- **Exception:** none.
- **Proof:** *verified* the cause survives: `new IllegalStateException(msg, e)`
  then `getCause()` returns the original. Expected disorder (a late frame, a
  reordered packet) is counted in a field, not thrown.
- **Enforced by:** review.

## 3. Messages that name the failure

- **Default:** `"Class.method: property value relation bound"`, e.g.
  `"Ring.push: gapNs -5 < 0"`. Every check's message is distinct, so a log line
  points at one line of code. Build the string only on the failing branch.
  Untrusted text in a message is sanitised: control characters to `?`, cut to a
  `CHARS_MAX`.
- **Banned:** a bare `throw new IllegalStateException()`; one message shared by two
  checks; `requireNonNull(x, "a" + b)` (the concatenation runs on every call, pass
  or fail).
- **Why:** a crash report from a user's phone is often the only evidence you get.
- **Exception:** none.
- **Proof:** *verified* `Objects.requireNonNull(null, msg)` throws NPE carrying
  `msg`; a test checks each message exactly (section 14). *Verified* the mutation
  probe: a mutated message with no test checking it survived (section 15).
- **Enforced by:** review plus the message tests.

## 4. No swallowed exceptions, a counted number of catch sites

- **Default:** a catch either handles the operating error, rethrows it with the
  cause, or is one of the project's named top-level catch sites. Those sites are
  listed in the design doc ("exactly N sites catch `RuntimeException`"). Each one
  fails the feature, not the host process: records the first fault, logs it once,
  disables the feature. `InterruptedException` restores the flag:
  `Thread.currentThread().interrupt();`.
- **Banned:** an empty catch, or one that only logs and carries on (CERT ERR00-J);
  `catch (Throwable)` and `catch (Error)` (OOM and `StackOverflowError` leave no
  state worth trusting); catching `Exception` anywhere but a named site.
- **Why:** a swallowed exception is a bug that hides until it corrupts something.
- **Exception:** CERT ERR00-J allows ignoring a failure while freeing a resource
  (a `close()` in cleanup); write the reason next to it.
- **Proof:** *verified* javac `-Xlint:all` has no empty-catch warning. The gate
  counts catch-alls: `grep -rnE 'catch \((Runtime)?Exception|catch \(Throwable'`
  must match the documented N.
- **Enforced by:** the grep count. Error Prone `EmptyCatch` (WARNING, *cited*)
  would catch empty blocks, but running it on AGP is **unconfirmed** (section 10).

## 5. A limit on everything — Power of 10 rules 2 and 3

> "Give all loops a fixed upper bound." / "Do not use dynamic memory allocation
> after initialization." — Holzmann

- **Default:**
  - Loops: `for (int i = 0; i < FRAMES_MAX; i++)`. Where the count is only known at
    run time, count iterations and throw past `ITERATIONS_MAX`.
  - Queues: `new ArrayBlockingQueue<>(QUEUE_CAPACITY)`, and an executor built with
    `new ThreadPoolExecutor(..., new ArrayBlockingQueue<>(N), rejectHandler)`.
  - Buffers and collections: fixed arrays or ring buffers sized at start-up, with a
    check before every add. Caches have a size cap and eviction.
  - Timeouts: every wait has one, as a named constant (`JOIN_TIMEOUT_MS`):
    `Future.get(t, unit)`, `Thread.join(ms)`, `poll(t, unit)`,
    `Socket.setSoTimeout`, `HttpURLConnection.setConnectTimeout`/`setReadTimeout`.
- **Banned:** `while (true)` with no cap; `new LinkedBlockingQueue<>()`;
  `Executors.newFixedThreadPool` / `newSingleThreadExecutor`; a growable
  `ArrayList`/`HashMap` fed by input with no cap; any untimed `get()`, `join()`,
  `take()`, `wait()`, `await()`, or `Thread.sleep` used as a wait.
- **Why:** unbounded means "until the phone runs out of memory or the user gives up".
- **Exception:** an event loop that must never end (a render thread): say so, and
  bound each pass instead.
- **Proof:** *verified* on Temurin 17: `new LinkedBlockingQueue<>()` and the queue
  inside `Executors.newFixedThreadPool(1)` both report `remainingCapacity()`
  2147483647; a new `Socket` has `soTimeout` 0 and a JDK `HttpURLConnection` has
  connect/read timeouts 0/0, and 0 means infinite (*cited*, Javadoc). Android's own
  `HttpURLConnection` defaults are **unconfirmed**; set them anyway.
- **Enforced by:** review; name the constant for each loop, queue and wait.

## 6. Short functions — Power of 10 rule 4

> "No function should be longer than what can be printed on a single sheet of
> paper … Typically, this means no more than about 60 lines of code per
> function." — Holzmann

- **Default:** 70 lines (TigerStyle). Branching stays in the parent, loops go into
  helpers.
- **Banned:** a method over 140 lines (the skill's hard limit).
- **Exception:** a generated or table-shaped method (a big `switch` over opcodes),
  with a comment saying why.
- **Proof:** line count per method in the diff.
- **Enforced by:** review. Neither javac nor Android Lint has a length check.

## 7. Layout and names

- **Default:**
  - New files: 100 columns (Google style §4.4, *cited*). Editing an existing file:
    follow the line length around you, even if it's 120 or 80. A diff that rewraps
    a file is noise.
  - Names: units and qualifiers last, in Java case. Fields and locals camelCase
    (`gapNs`, `timeoutMs`, `latencyMsMax`); constants UPPER_SNAKE
    (`RING_FRAMES`, `JOIN_TIMEOUT_MS`, `CHARS_MAX`), per Google style §5.2.4
    (*cited*).
  - Times are `long` nanoseconds from `System.nanoTime()`, named `...Ns`.
- **Banned:** a bare `timeout`, `size`, `delay`; mixing units in one expression
  without a conversion named in the code.
- **Why:** `latencyMsMax` and `latencyMsMin` line up; the unit is never a guess.
- **Exception:** Android API overrides keep the framework's names.
- **Proof:** `awk 'length > 100' NewFile.java` prints nothing for new files.
- **Enforced by:** review plus that awk line.

## 8. Explicit control flow — Power of 10 rule 1

> "Restrict all code to very simple control flow constructs—do not use goto
> statements, setjmp or longjmp constructs, or direct or indirect recursion."
> — Holzmann

- **Default:** loops and early `return`; a `switch` where every case ends in
  `break`/`return`/`throw` and `default` throws on the impossible case.
- **Banned:** recursion (direct, or through listeners and callbacks); `switch`
  fallthrough; labelled `break`/`continue` out of nested loops; exceptions used as
  control flow.
- **Why:** stack depth isn't yours to choose. *Verified*: a trivial recursion hit
  `StackOverflowError` at depth 40202 with the default stack and at 1479 with
  `-Xss256k`. Android threads get whatever stack the creator gave them.
- **Exception:** recursion over input finite by construction (a parsed tree with a
  depth cap), with the depth checked and a test at the cap.
- **Proof:** *verified* `-Xlint:all` warns on fallthrough. Recursion: review.
- **Enforced by:** javac `-Xlint:fallthrough` (in `all`); review for recursion.

## 9. Integers

- **Default:** `Math.addExact`/`multiplyExact`/`toIntExact` where overflow is a bug;
  `Math.floorDiv`/`floorMod` where the sign of the result matters.
- **Banned:** a narrowing cast `(int) someLong` without a range check; `Math.abs`
  on a value that may be `MIN_VALUE`.
- **Why:** Java wraps silently, always. *Verified*: `Integer.MAX_VALUE + 1` is
  -2147483648; `(int) 3_000_000_000L` is -1294967296; `-7 / 2` is -3 while
  `Math.floorDiv(-7, 2)` is -4; `Math.abs(Integer.MIN_VALUE)` is negative;
  `addExact` and `toIntExact` throw `ArithmeticException: integer overflow`.
- **Exception:** hashing and checksums, where wrap is the point; comment it.
- **Proof / Enforced by:** review.

## 10. Nullness and static analysis at zero — Power of 10 rule 10

> "All code must be compiled, from the first day of development, with all
> compiler warnings enabled at the most pedantic setting available. All code must
> compile without warnings." — Holzmann

- **Default:**
  - `@NonNull`/`@Nullable` from `androidx.annotation` on every parameter, return
    and field that crosses a class. Check at the boundary with `requireNonNull`.
  - javac `-Xlint:all` at zero; Android Lint at zero new issues (section 16).
- **Banned:** IntelliJ's `@NotNull` (Android lint reads only the androidx ones,
  *cited*); a blanket `@SuppressWarnings("all")`; `lintOptions { abortOnError false }`.
- **Why:** nullness is the bug class Java's type system leaves open.
- **Know the gaps:**
  - The command-line `lint` task "doesn't enforce nullness annotations … only
    Android Studio does this" (*cited*). So in CI the annotations are documentation
    plus Studio's Inspect Code, and `requireNonNull` is the real check. Lint's
    `UnknownNullness` is off by default (*verified* in the lint-checks jar).
  - NullAway (an Error Prone plugin) is the CI-able nullness check. On AGP it is
    **unconfirmed**: the Gradle Error Prone plugin says it has "no specific support
    for the Android Gradle Plugin", and current Error Prone needs JDK 21 (2.42.0 is
    the last for JDK 17), all *cited*.
  - *Verified*: plain javac, no flags, compiles raw types, a redundant cast,
    fallthrough and an empty catch silently with exit 0.
  - *Verified*: `-Xlint:all -Werror` shows 3 of the 4 warnings. The fallthrough
    one only appears once the earlier ones are fixed, so count with `-Xlint:all`
    alone, without `-Werror`.
- **Exception:** `@SuppressWarnings("<one id>")` on the smallest element, reason
  next to it. *Verified*: it suppresses only that id.
- **Proof / Enforced by:** javac and Android Lint in the gate.

## 11. Threads and the UI thread

- **Default:**
  - `volatile` for a flag or a published reference with one writer. Locks
    (`synchronized` or `ReentrantLock`) for anything read-modify-write or spanning
    two fields. `AtomicInteger`/`AtomicLong` for counters.
  - Every lock's comment says what it guards and the lock order:
    `// lock order: stateLock before ioLock`.
  - The UI thread does no disk, network, long compute, or `synchronized` on a lock
    a worker holds for long. Annotate with `@UiThread`/`@WorkerThread`.
  - `StrictMode` thread policy on in debug builds.
- **Banned:** `count++` on a `volatile`; holding a lock across a callback, I/O, or a
  blocking wait; two locks taken in an unstated order; non-volatile `long`/`double`
  shared across threads without a lock.
- **Why:** *verified*: two threads doing 1,000,000 `count++` each on a `volatile int`
  ended at 1062608 to 1122410 of 2000000 over three runs. JLS §17.4.5: a volatile
  write happens-before every later read of it; §17.7: non-volatile `long`/`double`
  writes may tear into two 32-bit halves (*cited*). An ANR fires when input gets
  no response within 5 seconds, and "main thread is blocked waiting for a
  synchronized block" is a listed cause (*cited*).
- **Exception:** none for the UI thread. Lock-free structures beyond the
  `java.util.concurrent` ones need a design doc.
- **Proof:** Lint `WrongThread` exists (*verified* in the jar) and checks the thread
  annotations. StrictMode is a best-effort developer tool, not a guarantee, and
  misses JNI (*cited*).
- **Enforced by:** Lint `WrongThread`, `StaticFieldLeak`, `HandlerLeak`; StrictMode
  in debug; review for lock order.

## 12. No allocation on hot paths

- **Default:** per-frame and per-event code (`onDraw`, `onTouchEvent`, render and
  audio loops) uses fields allocated at start-up: reused `Paint`/`Rect`, primitive
  arrays, `SparseArray`/`SparseIntArray` instead of `HashMap<Integer, ...>`.
  Index loops over `ArrayList` and arrays.
- **Banned:** `new` in `onDraw`; for-each over a `List` in a hot loop (allocates an
  `Iterator`); autoboxing (`List<Integer>`, `Map<Integer, ...>`); varargs calls;
  string concatenation or `String.format` for a log line that may be off;
  capturing lambdas created per call.
- **Why:** each allocation on a 16 ms frame is GC work stolen from the frame.
- **Exception:** start-up, error paths (rule 3's message string), tests.
- **Proof:** *verified* with `javap -c`: for-each over a `List<Integer>` compiles to
  `List.iterator()`, `Iterator.next()` and `Integer.intValue()` (an object and an
  unboxing per element). Lint IDs `DrawAllocation`, `UseSparseArrays`,
  `LogConditional` exist (*verified* in the lint-checks jar).
- **Enforced by:** Lint `DrawAllocation`, `UseSparseArrays`; review elsewhere.

## 13. Smallest scope, checked returns, no magic — Power of 10 rules 6–9

- **Scope:** fields `private final` unless they can't be; locals declared at first
  use. No mutable statics; a `static` holding a `Context` or `View` leaks the
  Activity (Lint `StaticFieldLeak`).
- **Return values:** check every boolean result that reports failure
  (`File.delete()`, `mkdirs()`, `renameTo()`, `Queue.offer()`); a `false` is an
  operating error. Mark your own with `@CheckResult`.
- **Reflection, annotation processing, `Unsafe`, JNI:** Java's preprocessor and
  pointers. Each use is named in the design doc with why; JNI boundaries check
  every argument on both sides.
- **Enforced by:** review; Lint for the leaks.

## 14. Testing

- **Default:**
  - Tests come after the contract is stable, never test-first: they pin down
    behaviour that was decided, not behaviour still moving.
  - Each test class opens with a goal-and-method comment:
    `// Goal: a full ring rejects pushes. Method: fill it, push once more.`
  - Worked examples from the design doc are exact-value tests (`assertEquals` on
    the number, not "greater than zero").
  - Every throwing check has a test that triggers it and checks the message:
    ```java
    IllegalStateException e = assertThrows(IllegalStateException.class, () -> ring.push(1));
    assertEquals("Ring.push: count 4 == RING_FRAMES 4", e.getMessage());
    ```
    (*verified* on Gradle 8.14.5 with JUnit 4.13.2; `assertThrows` is 4.13+, *cited*.)
  - Time is injected: the class takes a `LongSupplier nowNs`, the test advances it.
    Threads get a package-private synchronous step method (`writePass()`) so tests
    run without a thread at all; real-thread tests `join(JOIN_TIMEOUT_MS)` and
    assert the thread finished.
  - Randomised tests use a fixed seed, printed in the failure message.
- **Banned:** `Thread.sleep` in a test; asserting a thrown type without its
  message; tests that only check "no exception".
- **Why:** a test that sleeps is slow and still flaky; a message nobody checks
  rots.
- **Exception:** instrumented tests on a device for framework behaviour a JVM test
  can't fake; they still use timeouts, not sleeps.
- **Proof:** *verified*: `gradle test --tests 'probe.Nope*'` fails with "No tests
  found for given includes". *Verified*: with the test sources misplaced, the task
  reports `NO-SOURCE`, BUILD SUCCESSFUL, exit 0 and writes no result XML. Exit 0
  is not proof; the gate reads the XML.
- **Enforced by:** the gate (section 16) plus mutation (section 15).

## 15. Mutation before push

- **Default:** every new branch and check gets a mutant, and every mutant dies.
  pitest's Android plugin (`pl.droidsonroids.pitest`) needs AGP 8.5.1+ and JDK 17,
  and its dependency discovery doesn't work with Jetifier (*cited*). Below that,
  use a literal-replace script, `tools/mutate_<feature>.py`:
  ```python
  mutants = [  # (name, old, new): each old must occur exactly once
      ("full check >", "if (count == RING_FRAMES) {", "if (count > RING_FRAMES) {"),
      ("negative check off", "if (gapNs < 0) {", "if (false) {"),
  ]
  shutil.copy(source, backup)
  try:
      for name, old, new in mutants:
          text = backup.read_text()
          assert text.count(old) == 1, f"mutant '{name}' matches {text.count(old)} times"
          source.write_text(text.replace(old, new))
          result = subprocess.run([gradlew, "testDebugUnitTest"], cwd=project,
                                  capture_output=True, check=False, timeout=600)
          print(("killed   " if result.returncode != 0 else "SURVIVED ") + name)
  finally:
      shutil.copy(backup, source)
  ```
  Exit 1 if anything survived. Run with `python3 -B`.
- **Banned:** a survivor at push time; editing the source by hand or with `sed` to
  make a mutant (shell escaping silently matches nothing).
- **Why:** a test that passes on broken code tests nothing.
- **Exception:** an equivalent mutant (same behaviour); name it and drop it.
- **Proof:** *verified* on the scratch project: 3 of 4 mutants killed; the one that
  changed an `assert` message SURVIVED and the script exited 1. A mutant that
  doesn't compile counts as killed; write mutants that compile. A NO-SOURCE run
  exits 0, so it shows as SURVIVED, which errs the safe way.
- **Enforced by:** the gate, before push.

## 16. Gates

```fish
set -x JAVA_HOME (/usr/libexec/java_home -v 17)   # AGP 8.4.2 lint crashes on JBR 25
./gradlew lintDebug; or exit 1                     # baseline + abortOnError

# by message, not line number: a moved line can't hide a new issue
grep -E ': (Warning|Error): ' app/build/reports/lint-results-debug.txt \
    | sed -E 's/:[0-9]+: /: /' | sort > build/lint-now.txt
set new (comm -13 tools/lint-messages.baseline build/lint-now.txt)
test -z "$new"; or begin; printf '%s\n' $new; exit 1; end

./gradlew testDebugUnitTest; or exit 1
set xml app/build/test-results/testDebugUnitTest/TEST-*.xml
test (count $xml) -gt 0; or begin; echo "no test results (NO-SOURCE?)"; exit 1; end
grep -lE '(failures|errors)="[1-9]' $xml; and exit 1

python3 -B tools/mutate_<feature>.py; or exit 1    # before pushing
```

- The lint report line format `path:line: Warning: message [Id]` and the
  `lint-results-debug.{txt,xml,html}` paths are *verified* on AGP 8.4.2.
- Lint is not run by `assembleDebug`; it needs its own task (*cited*). Put this
  block in a committed `check.sh` (or `check.fish`) so "ready" is one command.
- javac warnings: with `-Werror` they fail `compileDebugJavaWithJavac` inside the
  test task. Without it, count `warning:` lines from the compile by message, same
  as lint.

## Sources

- Gerard J. Holzmann, "The Power of 10: Rules for Developing Safety-Critical Code",
  NASA/JPL, IEEE Computer, 2006: https://spinroot.com/gerard/pdf/P10.pdf. Quotes
  from the transcription in https://github.com/Vhivi/Powerof10-NASA.
- TigerBeetle, `TIGER_STYLE.md` (copy in `upstream/`): crash on corrupt code.
- JPL Institutional Coding Standard for Java, v1.1 (Havelund, Niessner, 2010; R01
  "compile with all checks, no warnings"): secondary copy only,
  https://www.scribd.com/document/326574148/JPL-Coding-Standard-Java-pdf. Official
  source not found.
- Android assert, D8/R8 strip: https://r8.googlesource.com/r8/+/e37b6cfccd444c50124e42ab5732975f5f27efca
- AGP 4.1, asserts enabled in debug builds:
  https://developer.android.com/build/releases/past-releases/agp-4-1-0-release-notes
- ART ignores `-ea`:
  https://android.googlesource.com/platform/art/+/a3fdf25f7d2800c7196422559ee1ea09cb74cc18/test/029-assert/expected-stdout.txt
- CERT MET01-J (no `assert` on public arguments):
  https://cmu-sei.github.io/secure-coding-standards/sei-cert-oracle-coding-standard-for-java/rules/methods-met/met01-j
- CERT EXP06-J (no side effects in asserts):
  https://cmu-sei.github.io/secure-coding-standards/sei-cert-oracle-coding-standard-for-java/rules/expressions-exp/exp06-j
- CERT ERR00-J (no suppressed exceptions; InterruptedException; cleanup exception):
  https://cmu-sei.github.io/secure-coding-standards/sei-cert-oracle-coding-standard-for-java/rules/exceptional-behavior-err/err00-j
- Effective Java 3rd ed., Item 49 "Check parameters for validity" (book; summary at
  https://dev.to/kylec32/effective-java-check-parameters-for-validity-28c0).
- Guava preconditions: https://github.com/google/guava/wiki/PreconditionsExplained
- JUnit 4.13 `assertThrows`: https://junit.org/junit4/javadoc/4.13/org/junit/Assert.html
- Google Java Style Guide (§4.4 column limit 100, §5.2.4 constant names):
  https://google.github.io/styleguide/javaguide.html
- `URLConnection` timeouts, zero means infinite:
  https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/net/URLConnection.html
- JLS 17 §17.4.5 and §17.7: https://docs.oracle.com/javase/specs/jls/se17/html/jls-17.html
- ANR triggers and causes: https://developer.android.com/topic/performance/vitals/anr
- StrictMode: https://developer.android.com/reference/android/os/StrictMode
- Android annotations (nullness not enforced by CLI lint, thread annotations):
  https://developer.android.com/studio/write/annotations
- Android Lint (baseline, abortOnError, `lintDebug`):
  https://developer.android.com/studio/write/lint
- Error Prone `EmptyCatch`: https://errorprone.info/bugpattern/EmptyCatch
- Error Prone JDK requirement: https://errorprone.info/docs/installation
- Gradle Error Prone plugin (no AGP support): https://github.com/tbroyer/gradle-errorprone-plugin
- NullAway: https://github.com/uber/NullAway
- pitest Android plugin: https://github.com/koral--/gradle-pitest-plugin
