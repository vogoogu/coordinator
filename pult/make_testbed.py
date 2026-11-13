"""Test workspace for Pult: five small projects with reference files, and their
test chats started in Claude Code (Opus and Sonnet).

    python make_testbed.py            create the workspace and start 5 chats
    python make_testbed.py --no-start only create the files
    python make_testbed.py D:\\tests   put the workspace into D:\\tests

The workspace goes to <home>\\pult-testbed. Existing files are not overwritten."""
import sys
from pathlib import Path

FILES = {
    "shop-site/SPEC.md": """# Landing page spec
Product: an online course for students who prepare for a university entrance exam.
Audience: Russian-speaking students, 17-22 years old, living abroad.
Must have: headline, 3 benefits, price block (3 plans), 3 reviews, sign-up button.
Tone: friendly, no hype. Language of the page: English.
""",
    "shop-site/index.html": "<!doctype html><title>TODO</title><h1>TODO</h1>\n",
    "tg-bot/NOTES.md": """# Telegram bot - notes
Goal: a funnel for new subscribers: welcome message, 3 questions, then a link to the course.
Done so far: bot.py answers /start with "hello". Nothing else.
Open question: which greeting text to use (formal or friendly)?
""",
    "tg-bot/bot.py": "def start():\n    return 'hello'\n",
    "data-report/data.csv": """month,signups,paid
2026-06,120,14
2026-07,180,25
2026-08,260,31
2026-09,410,52
2026-10,380,60
""",
    "refactor/messy.py": '''def f(a,b,c):
    r=[]
    for x in a:
        if x>b:
            r.append(x*c)
    s=0
    for y in r:
        s=s+y
    print("total",s)
    return s
def g(l):
    out={}
    for i in l:
        if i in out: out[i]=out[i]+1
        else: out[i]=1
    return out
''',
    "docs/README.md": "# docs\nEmpty on purpose. A chat will fill it.\n",
}

# (folder, model, first message, expected card on the board)
TASKS = [
    ("shop-site", "opus",
     "Read SPEC.md and build index.html from it. When you are finished, summarise in "
     "three bullets what you built and what is left.", "Done, check it"),
    ("tg-bot", "sonnet",
     "Read NOTES.md. Implement the funnel in bot.py, but before you write any code ask me "
     "whether the greeting should be formal or friendly. Wait for my answer.",
     "Waiting for you"),
    ("data-report", "opus",
     "Analyse data.csv and write report.md with the growth per month and the conversion "
     "from signup to paid. Take the target audience from ../shop-site/SPEC.md and write "
     "the report for that audience.", "Done, check it (reference to another project)"),
    ("refactor", "sonnet",
     "Refactor messy.py into small functions in a package, add unit tests, and explain "
     "every step as you go.", "Interrupted: press Esc after about 20 seconds, then close "
     "the window"),
    ("docs", "opus",
     "Read ../shop-site/SPEC.md, ../tg-bot/NOTES.md and ../data-report/data.csv and write "
     "README.md as a one-page overview of the whole workspace: what each project is, its "
     "state and the next step for each.", "Done, check it (references to three projects)"),
]


def prompts_text() -> str:
    out = ["# Test chats\n", "Started by Pult (Setup > Test workspace) or TEST.bat. "
           "To start one by hand: open a terminal in the folder and run "
           "`claude --model <model>`, then paste the message.\n"]
    for i, (folder, model, prompt, expect) in enumerate(TASKS, 1):
        out.append(f"{i}. {folder} ({model}) - expected: {expect}\n   {prompt}\n")
    out.append("Extra: open a second chat in shop-site (sonnet) and ask: Read index.html "
               "and list three things a reviewer would criticise. Two cards appear in the "
               "same project column.\n")
    return "\n".join(out)


def create(base=None) -> Path:
    root = Path(base or Path.home()) / "pult-testbed"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(text, encoding="utf-8")
    (root / "PROMPTS.md").write_text(prompts_text(), encoding="utf-8")
    return root


def launch(root: Path) -> list:
    """Start one Claude Code window per test project. Returns error texts."""
    from pult import start_claude
    errors = []
    for folder, model, prompt, _ in TASKS:
        err = start_claude(str(root / folder), model, prompt, f"test {folder} ({model})")
        if err:
            errors.append(err)
    return errors


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = create(args[0] if args else None)
    print(f"Test workspace: {root}\nPrompts: {root / 'PROMPTS.md'}")
    try:
        from pult import remember_folder
        remember_folder(root)
        print("Added to the Pult board.")
    except Exception as e:     # the board still works; the folder can be added by hand
        print(f"Could not add it to the board ({e}). Use '+ Add folder' in Pult.")
    if "--no-start" not in sys.argv:
        errors = launch(root)
        print("\n".join(errors) if errors else f"Started {len(TASKS)} Claude Code windows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
