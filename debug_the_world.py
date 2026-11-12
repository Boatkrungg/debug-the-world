# Debug the World -- a text adventure where every room is broken Python code.
#
# The kingdom of Pythonia is crumbling because of bugs. In each room you read a
# short broken function, repair it with the edit / insert / delete commands,
# then "run" the door's tests. Fix all 9 rooms to restore the world.
# Fewer hints and fewer failed attempts earn more points.
#
# Run it:       python3 debug_the_world.py
# All options:  python3 debug_the_world.py --help
# Verify it:    python3 debug_the_world.py --selftest
# Needs Python 3.7+ and nothing else (standard library only).
# Your code is executed in a separate, time-limited Python process.

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass

VERSION = "1.0"
TIME_LIMIT = 3.0          # seconds a room's code may run before the door gives up
MAX_LINES = 40            # most lines a room may contain
MAX_LINE_LEN = 200        # longest line a player may type
BASE_POINTS = 100
HINT_COST = 15
FREE_FAILS = 2            # failed runs that cost nothing
FAIL_COST = 5             # cost of each failed run after that
MIN_POINTS = 20           # a solved room is never worth less than this (unless given up)
UNDO_DEPTH = 50
DEFAULT_SAVE = "debug_the_world_save.json"

# --------------------------------------------------------------------------
# The sandbox harness. It runs in a child process: it compiles the player's
# code, executes it, evaluates each test expression and prints one JSON object.
# --------------------------------------------------------------------------
HARNESS = r'''
import io, json, sys, traceback
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
except Exception:
    pass

real_out = sys.stdout
data = json.loads(sys.stdin.read())
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()


def room_line(exc):
    line = None
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == "<room>":
            line = frame.lineno
    return line


def short(text, limit=160):
    return text if len(text) <= limit else text[:limit] + "..."


def same(got, expected):
    if isinstance(got, bool) != isinstance(expected, bool):
        return False
    try:
        return bool(got == expected)
    except BaseException:
        return False


def run():
    try:
        code = compile(data["source"], "<room>", "exec")
    except SyntaxError as exc:
        return {"status": "syntax", "kind": type(exc).__name__,
                "msg": exc.msg, "line": exc.lineno}
    except Exception as exc:
        return {"status": "error", "kind": type(exc).__name__,
                "msg": short(str(exc)), "line": None}
    namespace = {"__name__": "room"}
    try:
        exec(code, namespace)
    except BaseException as exc:
        return {"status": "error", "kind": type(exc).__name__,
                "msg": short(str(exc)), "line": room_line(exc)}
    results = []
    for test in data["tests"]:
        try:
            got = eval(compile(test["expr"], "<test>", "eval"), namespace)
        except BaseException as exc:
            results.append({"ok": False,
                            "error": type(exc).__name__ + ": " + short(str(exc)),
                            "line": room_line(exc)})
            continue
        try:
            shown = short(repr(got))
        except BaseException:
            shown = "<unprintable>"
        results.append({"ok": same(got, test["expected"]), "got": shown})
    return {"status": "ran", "results": results}


outcome = run()
real_out.write(json.dumps(outcome))
real_out.flush()
'''


# --------------------------------------------------------------------------
# World data
# --------------------------------------------------------------------------
@dataclass
class Test:
    expr: str
    expected: object
    hidden: bool = False


@dataclass
class Room:
    title: str
    story: str
    outro: str
    code: list
    solution: list
    tests: list
    hints: list
    walkthrough: list      # commands that turn `code` into `solution` (used by --selftest)


ROOMS = [
    Room(
        title="The Gate of Syntax",
        story=("A great stone gate bars the way into Pythonia. Its lock reads spells aloud, "
               "but the spell carved above it is garbled and the lock refuses to read it at all."),
        outro="The gate grinds open and a warm wind carries the smell of old books.",
        code=[
            'def open_gate(key):',
            '    if key == "moon"',
            '        return "The gate swings open."',
            '    return "The gate stays shut."',
        ],
        solution=[
            'def open_gate(key):',
            '    if key == "moon":',
            '        return "The gate swings open."',
            '    return "The gate stays shut."',
        ],
        tests=[
            Test('open_gate("moon")', "The gate swings open."),
            Test('open_gate("sun")', "The gate stays shut."),
            Test('open_gate("")', "The gate stays shut.", hidden=True),
        ],
        hints=[
            "Python can't even read this spell. When you run it, the error message names a line number.",
            "Look at line 2. Statements that open a block (if, for, while, def) must end with a particular punctuation mark.",
            'Line 2 needs a colon at the end:  edit 2 if key == "moon":',
        ],
        walkthrough=['edit 2 if key == "moon":'],
    ),
    Room(
        title="The Stepping Stones",
        story=("Beyond the gate lies a ravine. A spell should summon one glowing stone per step, "
               "1 to n, but it always comes up one stone short and the last stone never appears."),
        outro="Stones glow into place, all the way across the ravine.",
        code=[
            'def stepping_stones(n):',
            '    # Return the list [1, 2, ..., n]: one stone per step.',
            '    stones = []',
            '    for i in range(1, n):',
            '        stones.append(i)',
            '    return stones',
        ],
        solution=[
            'def stepping_stones(n):',
            '    # Return the list [1, 2, ..., n]: one stone per step.',
            '    stones = []',
            '    for i in range(1, n + 1):',
            '        stones.append(i)',
            '    return stones',
        ],
        tests=[
            Test('stepping_stones(5)', [1, 2, 3, 4, 5]),
            Test('stepping_stones(1)', [1]),
            Test('stepping_stones(0)', []),
            Test('stepping_stones(3)', [1, 2, 3], hidden=True),
        ],
        hints=[
            "The answer is almost right, just one stone short. This kind of bug has a famous name: off-by-one.",
            "range(a, b) stops *before* b. Which line uses range?",
            "Include n itself:  edit 4 for i in range(1, n + 1):",
        ],
        walkthrough=['edit 4 for i in range(1, n + 1):'],
    ),
    Room(
        title="The Library of Echoes",
        story=("Whatever you say in this library, the shelves repeat back. Today they are silent, "
               "and an angry scroll of red text hangs in the air."),
        outro="The shelves murmur your words back to you, louder and happier.",
        code=[
            'def echo(word, times):',
            '    result = []',
            '    for _ in range(times):',
            '        result.append(wrod)',
            '    return " ".join(result)',
        ],
        solution=[
            'def echo(word, times):',
            '    result = []',
            '    for _ in range(times):',
            '        result.append(word)',
            '    return " ".join(result)',
        ],
        tests=[
            Test('echo("hi", 3)', "hi hi hi"),
            Test('echo("ho", 1)', "ho"),
            Test('echo("x", 0)', ""),
            Test('echo("ab", 2)', "ab ab", hidden=True),
        ],
        hints=[
            "The error names a line and a word Python doesn't know. Read it carefully.",
            "Compare that word with the parameter names on line 1. Spot the difference?",
            "Line 4 has a typo:  edit 4 result.append(word)",
        ],
        walkthrough=['edit 4 result.append(word)'],
    ),
    Room(
        title="The Merchant's Ledger",
        story=("A merchant describes his loot with a ledger spell. It works beautifully "
               "until a number shows up, and then the ink bursts into flames."),
        outro="The ledger sighs happily and neatly lists every coin.",
        code=[
            'def describe_loot(name, gold):',
            '    return "The " + name + " holds " + gold + " gold."',
        ],
        solution=[
            'def describe_loot(name, gold):',
            '    return "The " + name + " holds " + str(gold) + " gold."',
        ],
        tests=[
            Test('describe_loot("chest", 50)', "The chest holds 50 gold."),
            Test('describe_loot("urn", 0)', "The urn holds 0 gold."),
            Test('describe_loot("hoard", 1200)', "The hoard holds 1200 gold.", hidden=True),
        ],
        hints=[
            "Read the error: it says which two kinds of things Python refused to combine.",
            "The + sign can glue text to text, but it can't glue a number onto text.",
            "Turn the number into text first:  edit 2 return \"The \" + name + \" holds \" + str(gold) + \" gold.\"",
        ],
        walkthrough=['edit 2 return "The " + name + " holds " + str(gold) + " gold."'],
    ),
    Room(
        title="The Clockwork Tower",
        story=("A tower whose gears should count down and then stop. Instead they spin forever, "
               "and the whole tower has started to shake."),
        outro="The gears click once, twice, and finally fall quiet. The tower stands still.",
        code=[
            'def countdown_total(n):',
            '    # Add up n + (n-1) + ... + 1 using a loop.',
            '    total = 0',
            '    while n > 0:',
            '        total += n',
            '    return total',
        ],
        solution=[
            'def countdown_total(n):',
            '    # Add up n + (n-1) + ... + 1 using a loop.',
            '    total = 0',
            '    while n > 0:',
            '        total += n',
            '        n -= 1',
            '    return total',
        ],
        tests=[
            Test('countdown_total(4)', 10),
            Test('countdown_total(1)', 1),
            Test('countdown_total(0)', 0),
            Test('countdown_total(100)', 5050, hidden=True),
        ],
        hints=[
            "If the tower never stops, a loop never ends. Which loop is it?",
            "A while loop repeats as long as its condition stays true. Does anything inside the loop ever change n?",
            "Add a line as the last line inside the loop:  insert 6 n -= 1",
        ],
        walkthrough=['insert 6 n -= 1'],
    ),
    Room(
        title="The Sphinx's Calendar",
        story=("A sphinx guards the calendar and tells travellers whether a year is a leap year. "
               "For centuries it has been wrong about some of them, and it is a little embarrassed."),
        outro="The sphinx purrs, flips a page, and lets you pass.",
        code=[
            'def is_leap_year(year):',
            '    if year % 4 == 0:',
            '        return True',
            '    return False',
        ],
        solution=[
            'def is_leap_year(year):',
            '    if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):',
            '        return True',
            '    return False',
        ],
        tests=[
            Test('is_leap_year(2024)', True),
            Test('is_leap_year(2023)', False),
            Test('is_leap_year(1900)', False),
            Test('is_leap_year(2000)', True),
            Test('is_leap_year(2100)', False, hidden=True),
        ],
        hints=[
            "Run it and see which year the Sphinx gets wrong. What is special about that year?",
            "Century years (divisible by 100) are NOT leap years, unless they are also divisible by 400.",
            "Line 2 needs the full rule:  edit 2 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):",
        ],
        walkthrough=['edit 2 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):'],
    ),
    Room(
        title="The Potion Cellar",
        story=("Potions bubble on every shelf. The alchemist's averaging spell gives crude answers, "
               "and when a shelf is empty it explodes. Two separate bugs hide here."),
        outro="The potions settle into a calm, precise shimmer.",
        code=[
            'def average_potency(potions):',
            '    total = 0',
            '    for p in potions:',
            '        total += p',
            '    return total // len(potions)',
        ],
        solution=[
            'def average_potency(potions):',
            '    if not potions:',
            '        return 0',
            '    total = 0',
            '    for p in potions:',
            '        total += p',
            '    return total / len(potions)',
        ],
        tests=[
            Test('average_potency([1, 2])', 1.5),
            Test('average_potency([4, 4, 4])', 4.0),
            Test('average_potency([])', 0),
            Test('average_potency([3, 4, 5, 6])', 4.5, hidden=True),
        ],
        hints=[
            "Run it. One test gives a wrong number and another crashes. Those are two different bugs.",
            "// is floor division and rounds down. And what is len([])? What happens if you divide by it?",
            "Use / instead of //, and return early for an empty list: if not potions: / return 0",
        ],
        walkthrough=[
            'edit 5 return total / len(potions)',
            'insert 2 if not potions:',
            'insert 3 return 0',
        ],
    ),
    Room(
        title="The Troll's Purse",
        story=("A troll collects tolls in a purse. Strangely, coins from yesterday's travellers "
               "keep turning up in today's purse. (Note: the door's trials run in order, in one session.)"),
        outro="The troll counts a fresh, honest purse and grumbles you through.",
        code=[
            'def add_toll(coin, purse=[]):',
            '    purse.append(coin)',
            '    return purse',
        ],
        solution=[
            'def add_toll(coin, purse=None):',
            '    if purse is None:',
            '        purse = []',
            '    purse.append(coin)',
            '    return purse',
        ],
        tests=[
            Test('add_toll(1)', [1]),
            Test('add_toll(2)', [2]),
            Test('add_toll(3, [9])', [9, 3]),
            Test('add_toll(4)', [4], hidden=True),
        ],
        hints=[
            "Each call without a purse should start a brand-new purse. Where does the purse come from on line 1?",
            "A default value like purse=[] is created ONCE, when the function is defined, then shared by every call. A famous Python trap.",
            "Default to None and build the list inside:  edit 1 def add_toll(coin, purse=None):  then  insert 2 if purse is None:  then  insert 3 purse = []",
        ],
        walkthrough=[
            'edit 1 def add_toll(coin, purse=None):',
            'insert 2 if purse is None:',
            'insert 3 purse = []',
        ],
    ),
    Room(
        title="The Heart of the World",
        story=("At the centre of Pythonia floats a glowing sphere that counts every word spoken in the "
               "kingdom. It keeps losing count, and it treats 'Tea' and 'tea' as total strangers."),
        outro=("The sphere blazes gold. Every bug in the kingdom is gone, and Pythonia is whole again."),
        code=[
            'def tally(words):',
            '    counts = {}',
            '    for w in words:',
            '        counts[w] = 1',
            '    return counts',
        ],
        solution=[
            'def tally(words):',
            '    counts = {}',
            '    for w in words:',
            '        w = w.lower()',
            '        counts[w] = counts.get(w, 0) + 1',
            '    return counts',
        ],
        tests=[
            Test('tally(["a", "b", "a"])', {"a": 2, "b": 1}),
            Test('tally(["Tea", "tea", "TEA"])', {"tea": 3}),
            Test('tally([])', {}),
            Test('tally(["Ab", "aB", "c"])', {"ab": 2, "c": 1}, hidden=True),
        ],
        hints=[
            "Two bugs again. Run it and look at what the count for 'a' comes out as.",
            "Line 4 *sets* the count to 1 every time instead of adding to it. Also, 'Tea' and 'tea' should be the same word.",
            "Lower-case each word first (insert 4 w = w.lower()), then count with  edit 5 counts[w] = counts.get(w, 0) + 1",
        ],
        walkthrough=[
            'insert 4 w = w.lower()',
            'edit 5 counts[w] = counts.get(w, 0) + 1',
        ],
    ),
]

PROLOGUE = (
    "Pythonia was built entirely out of code, and someone shipped it with bugs. Doors refuse to open, "
    "bridges fall short, clocks never stop. You are the kingdom's last Debugger. In every room you will "
    "read the broken code, repair it, and run the door's tests. Mend all {n} rooms to save the world."
)

ERROR_HELP = {
    "NameError": "Python met a name it doesn't know. Check spelling against your variables and parameters.",
    "TypeError": "Two things of incompatible types were combined.",
    "ZeroDivisionError": "Something was divided by zero.",
    "IndentationError": "The indentation (leading spaces) doesn't line up.",
    "RecursionError": "A function kept calling itself without stopping.",
    "MemoryError": "The code tried to use far too much memory.",
    "IndexError": "A list position was used that doesn't exist.",
    "KeyError": "A dictionary key was looked up that isn't there.",
    "AttributeError": "Something was asked to do a trick it doesn't know.",
}

ALIASES = {
    "h": "help", "?": "help", "l": "look", "c": "code", "t": "tests", "r": "run",
    "e": "edit", "i": "insert", "d": "delete", "del": "delete", "u": "undo",
    "m": "map", "s": "score", "q": "quit", "exit": "quit", "give-up": "giveup",
    "restart": "reset", "go": "goto",
}
COMMAND_NAMES = ["help", "look", "code", "tests", "run", "edit", "insert", "delete", "undo",
                 "reset", "hint", "giveup", "map", "goto", "score", "quit"]

STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
          "blue": "34", "magenta": "35", "cyan": "36"}

HELP_TEXT = """\
Commands
  look            re-read the story of this room
  code            show the broken code with line numbers
  tests           show what the door demands
  run             try the door: runs the tests on your current code
  edit N TEXT     replace line N with TEXT
  insert N TEXT   add a new line before line N (use last line + 1 to append)
  delete N        remove line N
  undo            take back your last change
  reset           restore this room's original broken code
  hint            reveal the next hint (costs {hint} points)
  giveup          let the spirits fix the room for you (scores 0)
  map             see the kingdom and your progress
  goto N          revisit a room you have already reached
  score           show your points
  quit            save and leave

Indentation
  edit keeps the old line's indentation. insert guesses it from the line above.
  To set indentation yourself, start TEXT with | and type the line exactly as
  it should appear, for example:
      edit 4 |        total += n

Scoring
  Each room is worth {base} points. Every hint costs {hint}. After {free} failed runs,
  each further failed run costs {fail}. A solved room is never worth less than {min}.\
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def pick_symbols(stream):
    enc = (getattr(stream, "encoding", None) or "").lower()
    if "utf" in enc:
        return {"ok": "\u2714", "bad": "\u2718", "bar": "\u2502", "here": "\u25c4", "rule": "\u2550"}
    return {"ok": "OK", "bad": "X", "bar": "|", "here": "<-", "rule": "="}


def as_source(lines):
    return "\n".join(lines) + "\n"


def run_source(source, tests):
    """Run `source` against `tests` in a child process and return the outcome dict."""
    payload = json.dumps({
        "source": source,
        "tests": [{"expr": t.expr, "expected": t.expected} for t in tests],
    })
    try:
        proc = subprocess.run(
            [sys.executable or "python3", "-I", "-c", HARNESS],
            input=payload, capture_output=True, text=True, timeout=TIME_LIMIT,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except OSError as exc:
        return {"status": "internal", "msg": str(exc)}
    try:
        outcome = json.loads(proc.stdout)
        if not isinstance(outcome, dict):
            raise ValueError
        return outcome
    except ValueError:
        tail = (proc.stderr or "").strip().splitlines()
        return {"status": "crash", "msg": tail[-1] if tail else ""}


def all_passed(outcome, tests):
    if outcome.get("status") != "ran":
        return False
    results = outcome.get("results", [])
    return len(results) == len(tests) and all(r.get("ok") for r in results)


def leading_spaces(line):
    return line[: len(line) - len(line.lstrip(" "))]


def auto_indent(lines, index):
    """Guess the indentation for a new line placed at 0-based `index`."""
    if index <= 0 or index > len(lines):
        return ""
    prev = lines[index - 1]
    indent = leading_spaces(prev)
    if prev.split("#")[0].rstrip().endswith(":"):
        indent += "    "
    return indent


# --------------------------------------------------------------------------
# Game state
# --------------------------------------------------------------------------
@dataclass
class Record:
    solved: bool = False
    points: int = 0
    hints: int = 0
    fails: int = 0
    peeked: bool = False


class Game:
    def __init__(self, save_path=None, color=False, out=None):
        self.save_path = save_path
        self.out = out if out is not None else sys.stdout
        self.color = color
        self.sym = pick_symbols(self.out)
        self.records = [Record() for _ in ROOMS]
        self.sources = {}
        self.undo_stack = {}
        self.current = 0
        self.running = True
        self.save_warned = False
        self.dispatch = {
            "help": self.cmd_help, "look": self.cmd_look, "code": self.cmd_code,
            "tests": self.cmd_tests, "run": self.cmd_run, "edit": self.cmd_edit,
            "insert": self.cmd_insert, "delete": self.cmd_delete, "undo": self.cmd_undo,
            "reset": self.cmd_reset, "hint": self.cmd_hint, "giveup": self.cmd_giveup,
            "map": self.cmd_map, "goto": self.cmd_goto, "score": self.cmd_score,
            "quit": self.cmd_quit,
        }

    # ---- output helpers --------------------------------------------------
    def paint(self, text, *names):
        if not self.color or not names:
            return text
        codes = ";".join(STYLES[n] for n in names)
        return "\033[%sm%s\033[0m" % (codes, text)

    def say(self, text=""):
        print(text, file=self.out)

    def para(self, text, indent=""):
        width = max(40, min(shutil.get_terminal_size((80, 24)).columns, 80)) - 2
        self.say(textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent))

    def ask(self, prompt):
        try:
            return input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    # ---- derived state ---------------------------------------------------
    @property
    def room(self):
        return ROOMS[self.current]

    @property
    def rec(self):
        return self.records[self.current]

    @property
    def lines(self):
        return self.sources.setdefault(self.current, list(self.room.code))

    def frontier(self):
        """Index of the furthest room the player may enter."""
        for i, rec in enumerate(self.records):
            if not rec.solved:
                return i
        return len(ROOMS) - 1

    def everything_solved(self):
        return all(r.solved for r in self.records)

    def total_points(self):
        return sum(r.points for r in self.records)

    @staticmethod
    def points_for(rec):
        penalty = HINT_COST * rec.hints + FAIL_COST * max(0, rec.fails - FREE_FAILS)
        return max(MIN_POINTS, BASE_POINTS - penalty)

    # ---- persistence -----------------------------------------------------
    def save(self):
        """Write progress. Returns True if saved, False if it failed, None if saving is off."""
        if not self.save_path:
            return None
        data = {
            "version": 1,
            "current": self.current,
            "records": [asdict(r) for r in self.records],
            "sources": {str(k): v for k, v in self.sources.items() if v != ROOMS[k].code},
        }
        tmp = self.save_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.save_path)
            return True
        except OSError as exc:
            if not self.save_warned:
                self.save_warned = True
                self.say(self.paint("Warning: could not save progress (%s)." % exc, "yellow"))
            return False

    def load(self):
        """Load saved progress. Returns True if a save was restored."""
        if not self.save_path or not os.path.exists(self.save_path):
            return False
        try:
            with open(self.save_path, encoding="utf-8") as fh:
                data = json.load(fh)
            raw_records = data["records"]
            if not isinstance(raw_records, list) or len(raw_records) != len(ROOMS):
                raise ValueError("wrong number of rooms")
            records = []
            for raw in raw_records:
                records.append(Record(
                    solved=bool(raw["solved"]),
                    points=min(BASE_POINTS, max(0, int(raw["points"]))),
                    hints=min(3, max(0, int(raw["hints"]))),
                    fails=max(0, int(raw["fails"])),
                    peeked=bool(raw["peeked"]),
                ))
            current = int(data["current"])
            sources = {}
            for key, value in data.get("sources", {}).items():
                idx = int(key)
                if (0 <= idx < len(ROOMS) and isinstance(value, list) and len(value) <= MAX_LINES
                        and all(isinstance(v, str) for v in value)):
                    sources[idx] = value
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reason = exc if isinstance(exc, (OSError, ValueError)) else "unexpected format"
            self.say(self.paint("Your save file couldn't be read (%s); starting fresh." % reason, "yellow"))
            try:
                os.replace(self.save_path, self.save_path + ".corrupt")
            except OSError:
                pass
            return False
        self.records = records
        self.sources = sources
        self.current = min(max(0, current), self.frontier())
        return True

    # ---- display ---------------------------------------------------------
    def rule(self, title=""):
        bar = self.sym["rule"] * 3
        self.say(self.paint("%s %s %s" % (bar, title, bar), "bold", "cyan"))

    def show_intro(self):
        self.say()
        self.rule("Room %d of %d: %s" % (self.current + 1, len(ROOMS), self.room.title))
        self.para(self.room.story)
        self.say()
        self.say(self.paint("Type 'code' to see the problem, or 'help' for commands.", "dim"))

    def show_code(self):
        lines = self.lines
        width = len(str(max(len(lines), 1)))
        self.say()
        for n, text in enumerate(lines, 1):
            self.say("  %s %s %s" % (self.paint(str(n).rjust(width), "dim"), self.sym["bar"], text))
        if not lines:
            self.say(self.paint("  (the page is blank)", "dim"))
        self.say()

    # ---- command loop ----------------------------------------------------
    def prompt(self):
        return "[%d/%d %s] > " % (self.current + 1, len(ROOMS), self.room.title)

    def farewell(self, lead=""):
        result = self.save()
        if result:
            ending = "Progress saved. Farewell, Debugger."
        elif result is None:
            ending = "Farewell, Debugger."
        else:
            ending = "Farewell, Debugger. (Your progress could not be saved.)"
        self.say((lead + " " if lead else "") + ending)

    def loop(self):
        while self.running:
            try:
                raw = input(self.prompt())
            except EOFError:
                print()
                self.farewell("The lights flicker out.")
                return
            except KeyboardInterrupt:
                print()
                self.farewell("Interrupted.")
                return
            self.handle(raw)
            self.save()

    def handle(self, raw):
        parts = raw.split(None, 1)
        if not parts:
            return
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        cmd = ALIASES.get(cmd, cmd)
        action = self.dispatch.get(cmd)
        if action is None:
            near = difflib.get_close_matches(cmd, COMMAND_NAMES, n=1)
            tip = " Did you mean '%s'?" % near[0] if near else ""
            self.say("I don't know the command '%s'.%s Type 'help' for the list." % (parts[0][:30], tip))
            return
        action(rest)

    # ---- simple commands -------------------------------------------------
    def cmd_help(self, rest=""):
        self.say(HELP_TEXT.format(hint=HINT_COST, base=BASE_POINTS, free=FREE_FAILS,
                                  fail=FAIL_COST, min=MIN_POINTS))

    def cmd_look(self, rest=""):
        self.show_intro()

    def cmd_code(self, rest=""):
        self.show_code()

    def cmd_tests(self, rest=""):
        self.say()
        self.say(self.paint("The door demands:", "bold"))
        hidden = 0
        shown = 0
        for t in self.room.tests:
            if t.hidden:
                hidden += 1
                continue
            shown += 1
            self.say("  %d. %s  ->  %r" % (shown, t.expr, t.expected))
        if hidden:
            self.say("  + %d unseen trial%s" % (hidden, "" if hidden == 1 else "s"))
        self.say(self.paint("Trials run in order, all within one session.", "dim"))
        self.say()

    def cmd_map(self, rest=""):
        front = self.frontier()
        self.say()
        for i, room in enumerate(ROOMS):
            rec = self.records[i]
            if rec.solved:
                status = self.paint("%s %d pts%s" % (self.sym["ok"], rec.points, " (peeked)" if rec.peeked else ""), "green")
            elif i <= front:
                status = self.paint("in progress", "yellow")
            else:
                status = self.paint("locked", "dim")
            title = room.title if i <= front else "???"
            here = "  " + self.sym["here"] + " you are here" if i == self.current else ""
            self.say("  %d. %-26s %s%s" % (i + 1, title, status, here))
        self.say()

    def cmd_score(self, rest=""):
        solved = sum(1 for r in self.records if r.solved)
        self.say("Rooms restored: %d/%d   Points: %d/%d" % (
            solved, len(ROOMS), self.total_points(), BASE_POINTS * len(ROOMS)))
        if not self.rec.solved:
            self.say("This room is worth %d points if you solve it now (hints used: %d, failed runs: %d)." % (
                self.points_for(self.rec), self.rec.hints, self.rec.fails))

    def cmd_quit(self, rest=""):
        self.farewell()
        self.running = False

    def cmd_goto(self, rest=""):
        token = rest.strip()
        if not (token.isascii() and token.isdigit()):
            self.say("Usage: goto N   (N is a room number from 1 to %d; see 'map')" % len(ROOMS))
            return
        n = int(token)
        if not 1 <= n <= len(ROOMS):
            self.say("There is no room %d. Rooms run from 1 to %d." % (n, len(ROOMS)))
            return
        if n - 1 > self.frontier():
            self.say("Room %d is still sealed. Restore the earlier rooms first." % n)
            return
        self.current = n - 1
        self.show_intro()

    # ---- editing ---------------------------------------------------------
    def push_undo(self):
        stack = self.undo_stack.setdefault(self.current, [])
        stack.append(list(self.lines))
        del stack[:-UNDO_DEPTH]

    def parse_number(self, token, low, high, what):
        if not (token.isascii() and token.isdigit()):
            self.say("'%s' is not a line number. %s" % (token[:20], what))
            return None
        n = int(token)
        if not low <= n <= high:
            self.say("Line %d is out of range. %s" % (n, what))
            return None
        return n

    def build_line(self, raw, default_indent):
        """Turn what the player typed into a line of code, or None (after explaining why)."""
        raw = raw.replace("\t", "    ")
        if raw.startswith("|"):
            text = raw[1:].rstrip()
        else:
            text = default_indent + raw.strip()
        if not text.strip():
            self.say("That line is empty. Use 'delete N' to remove a line.")
            return None
        if len(text) > MAX_LINE_LEN:
            self.say("That line is too long (limit %d characters)." % MAX_LINE_LEN)
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            self.say("That line contains control characters, which the door can't read.")
            return None
        return text

    def cmd_edit(self, rest=""):
        parts = rest.split(None, 1)
        if len(parts) < 2:
            self.say("Usage: edit N TEXT   e.g.  edit 2 if key == \"moon\":")
            return
        lines = self.lines
        n = self.parse_number(parts[0], 1, len(lines), "This room has %d lines." % len(lines))
        if n is None:
            return
        new = self.build_line(parts[1], leading_spaces(lines[n - 1]))
        if new is None:
            return
        self.push_undo()
        lines[n - 1] = new
        self.show_code()

    def cmd_insert(self, rest=""):
        parts = rest.split(None, 1)
        if len(parts) < 2:
            self.say("Usage: insert N TEXT   (adds a new line before line N)")
            return
        lines = self.lines
        if len(lines) >= MAX_LINES:
            self.say("The page is full (limit %d lines). Delete something first." % MAX_LINES)
            return
        n = self.parse_number(parts[0], 1, len(lines) + 1,
                              "Use 1 to %d (%d appends at the end)." % (len(lines) + 1, len(lines) + 1))
        if n is None:
            return
        new = self.build_line(parts[1], auto_indent(lines, n - 1))
        if new is None:
            return
        self.push_undo()
        lines.insert(n - 1, new)
        self.show_code()

    def cmd_delete(self, rest=""):
        lines = self.lines
        token = rest.strip()
        if not token:
            self.say("Usage: delete N")
            return
        n = self.parse_number(token, 1, len(lines), "This room has %d lines." % len(lines))
        if n is None:
            return
        self.push_undo()
        del lines[n - 1]
        self.show_code()

    def cmd_undo(self, rest=""):
        stack = self.undo_stack.get(self.current, [])
        if not stack:
            self.say("Nothing to undo in this room.")
            return
        self.sources[self.current] = stack.pop()
        self.show_code()

    def cmd_reset(self, rest=""):
        if self.lines == self.room.code:
            self.say("This room is already in its original state.")
            return
        self.push_undo()
        self.sources[self.current] = list(self.room.code)
        self.say("The room is back to its original broken state (you can still 'undo').")
        self.show_code()

    # ---- hints and giving up --------------------------------------------
    def cmd_hint(self, rest=""):
        rec, room = self.rec, self.room
        if rec.solved:
            self.say("This room is already restored, so no hint is needed.")
            return
        if rec.hints >= len(room.hints):
            self.say("You have seen every hint. The last one was:")
            self.para(room.hints[-1], "  ")
            return
        rec.hints += 1
        self.say(self.paint("Hint %d/%d (-%d points):" % (rec.hints, len(room.hints), HINT_COST), "yellow"))
        self.para(room.hints[rec.hints - 1], "  ")

    def cmd_giveup(self, rest=""):
        if self.rec.solved:
            self.say("This room is already restored.")
            return
        answer = self.ask("Let the spirits repair this room? It will score 0 points. (y/N) ")
        if answer not in ("y", "yes"):
            self.say("Good. Keep going, you can do this.")
            return
        self.sources[self.current] = list(self.room.solution)
        self.rec.solved = True
        self.rec.peeked = True
        self.rec.points = 0
        self.say("The spirits rewrite the code for you:")
        self.show_code()
        self.after_solve()

    # ---- running the door ------------------------------------------------
    def cmd_run(self, rest=""):
        room, rec = self.room, self.rec
        self.say(self.paint("The door examines your code...", "dim"))
        outcome = run_source(as_source(self.lines), room.tests)
        passed = self.report(outcome)
        if passed:
            if rec.solved:
                self.say("The way is already open (no new points for a replay).")
            else:
                rec.solved = True
                rec.points = self.points_for(rec)
                self.say(self.paint("\n%s The door swings open! +%d points" % (self.sym["ok"], rec.points), "bold", "green"))
                self.after_solve()
        else:
            if not rec.solved:
                rec.fails += 1
                over = rec.fails - FREE_FAILS
                if over > 0:
                    self.say(self.paint("(Failed run #%d: -%d points)" % (rec.fails, FAIL_COST), "dim"))
            self.say(self.paint("Not yet. Read the messages above, then edit and run again. Try 'hint' if stuck.", "dim"))

    def report(self, outcome):
        """Print the outcome of a run. Returns True when every trial passed."""
        status = outcome.get("status")
        tests = self.room.tests
        lines = self.lines
        bad = self.sym["bad"]
        if status in ("syntax", "error"):
            kind = outcome.get("kind", "Error")
            line = outcome.get("line")
            where = " (line %d)" % line if isinstance(line, int) else ""
            self.say(self.paint("%s The door can't read your spell: %s: %s%s" % (
                bad, kind, outcome.get("msg", ""), where), "red"))
            if isinstance(line, int) and 1 <= line <= len(lines):
                self.say("    %d %s %s" % (line, self.sym["bar"], lines[line - 1]))
            if kind in ERROR_HELP:
                self.say(self.paint("    " + ERROR_HELP[kind], "dim"))
            return False
        if status == "timeout":
            self.say(self.paint("%s The code ran for over %d seconds without finishing." % (bad, int(TIME_LIMIT)), "red"))
            self.say(self.paint("    Is there a loop that never ends?", "dim"))
            return False
        if status in ("crash", "internal"):
            self.say(self.paint("%s The spell collapsed violently before it could report back." % bad, "red"))
            if outcome.get("msg"):
                self.say("    " + str(outcome["msg"])[:200])
            return False
        results = outcome.get("results", [])
        if len(results) != len(tests):
            self.say(self.paint("%s The door gave a garbled answer. Try again." % bad, "red"))
            return False
        passed = 0
        for test, result in zip(tests, results):
            label = "an unseen trial" if test.hidden else test.expr
            if result.get("ok"):
                passed += 1
                self.say(self.paint("  %s %s" % (self.sym["ok"], label), "green"))
                continue
            self.say(self.paint("  %s %s" % (bad, label), "red"))
            if "error" in result:
                line = result.get("line")
                where = " (your code, line %d)" % line if isinstance(line, int) else ""
                self.say("      raised %s%s" % (result["error"], where))
                kind = result["error"].split(":")[0]
                if kind in ERROR_HELP:
                    self.say(self.paint("      " + ERROR_HELP[kind], "dim"))
            elif test.hidden:
                self.say("      this trial failed (its details stay hidden)")
            else:
                self.say("      expected %r but got %s" % (test.expected, result.get("got", "?")))
        self.say("  %d of %d trials passed." % (passed, len(tests)))
        return passed == len(tests)

    def after_solve(self):
        self.say()
        self.para(self.room.outro)
        if self.everything_solved():
            self.finale()
            return
        self.current = self.frontier()
        self.show_intro()

    def finale(self):
        total = self.total_points()
        top = BASE_POINTS * len(ROOMS)
        ratio = total / top
        if ratio >= 0.9:
            rank = "Grand Archdebugger"
        elif ratio >= 0.7:
            rank = "Master Debugger"
        elif ratio >= 0.4:
            rank = "Journeyman Debugger"
        else:
            rank = "Apprentice Debugger"
        self.say()
        self.rule("PYTHONIA IS RESTORED")
        self.say("Final score: %d / %d" % (total, top))
        self.say("Your rank:   %s" % rank)
        self.say()
        self.say("Type 'map' to review your journey, 'goto N' to revisit a room, or 'quit' to leave.")

    # ---- startup ---------------------------------------------------------
    def welcome(self, resumed):
        self.say()
        self.say(self.paint("  D E B U G   T H E   W O R L D", "bold", "magenta"))
        self.say(self.paint("  a text adventure in broken Python  (v%s)" % VERSION, "dim"))
        self.say()
        if resumed:
            self.say("Welcome back, Debugger. Restored %d of %d rooms." % (
                sum(1 for r in self.records if r.solved), len(ROOMS)))
        else:
            self.para(PROLOGUE.format(n=len(ROOMS)))
        if resumed and self.everything_solved():
            self.finale()
        else:
            self.show_intro()


# --------------------------------------------------------------------------
# Self-test and entry point
# --------------------------------------------------------------------------
def selftest():
    """Check every room: the original is broken, the walkthrough fixes it exactly."""
    problems = []
    for number, room in enumerate(ROOMS, 1):
        label = "Room %d (%s)" % (number, room.title)
        if len(room.hints) != 3:
            problems.append("%s: needs exactly 3 hints" % label)
        if room.code == room.solution:
            problems.append("%s: original code equals the solution" % label)
        for t in room.tests:
            try:
                json.dumps(t.expected)
                compile(t.expr, "<test>", "eval")
            except (TypeError, ValueError, SyntaxError) as exc:
                problems.append("%s: bad test %r (%s)" % (label, t.expr, exc))
        if all_passed(run_source(as_source(room.code), room.tests), room.tests):
            problems.append("%s: the broken code already passes" % label)
        if not all_passed(run_source(as_source(room.solution), room.tests), room.tests):
            problems.append("%s: the reference solution fails its tests" % label)
        game = Game(save_path=None, color=False, out=io.StringIO())
        game.current = number - 1
        for command in room.walkthrough:
            game.handle(command)
        if game.lines != room.solution:
            problems.append("%s: walkthrough does not produce the solution exactly" % label)
        elif not all_passed(run_source(as_source(game.lines), room.tests), room.tests):
            problems.append("%s: walkthrough result fails its tests" % label)
        print("  %s %s" % ("ok " if not any(p.startswith(label) for p in problems) else "BAD", label))
    if problems:
        print("\nSelf-test FAILED:")
        for p in problems:
            print("  - " + p)
        return 1
    print("\nSelf-test passed: all %d rooms behave as designed." % len(ROOMS))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="debug_the_world.py",
        description="Debug the World: a text adventure where every room is broken Python code.",
    )
    parser.add_argument("--list", action="store_true", help="list the rooms and exit")
    parser.add_argument("--room", type=int, metavar="N", help="start in room N (must already be unlocked)")
    parser.add_argument("--save", default=DEFAULT_SAVE, metavar="FILE",
                        help="save file to use (default: %s)" % DEFAULT_SAVE)
    parser.add_argument("--no-save", action="store_true", help="play without reading or writing a save file")
    parser.add_argument("--reset", action="store_true", help="erase saved progress and start over")
    parser.add_argument("--no-color", action="store_true", help="disable colored output")
    parser.add_argument("--selftest", action="store_true", help="verify that every room works, then exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()
    if args.list:
        for i, room in enumerate(ROOMS, 1):
            print("%d. %s" % (i, room.title))
        return 0
    if args.room is not None and not 1 <= args.room <= len(ROOMS):
        build_parser().error("--room must be between 1 and %d" % len(ROOMS))

    use_color = (sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
                 and os.environ.get("TERM") != "dumb" and os.name != "nt")
    save_path = None if args.no_save else args.save
    game = Game(save_path=save_path, color=use_color)

    if args.reset and save_path:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
                print("Saved progress erased.")
        except OSError as exc:
            print("Could not erase the save file: %s" % exc)

    resumed = game.load()
    if args.room is not None:
        if args.room - 1 > game.frontier():
            print("Room %d is still sealed; starting at room %d instead." % (args.room, game.frontier() + 1))
        else:
            game.current = args.room - 1
    game.welcome(resumed)
    game.loop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
