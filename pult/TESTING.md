# How to test Pult with your own Claude Code

Goal: produce many real chats on Opus and Sonnet, with references between files
and projects, and check that Pult shows them correctly.

## 1. Start everything with one click

Double-click `TEST.bat` (or in Pult: **Setup > Test workspace > Create and
start 5 test chats**). It:

- creates `pult-testbed` in your home folder: five small projects (shop-site,
  tg-bot, data-report, refactor, docs) with spec and note files;
- adds the folder to the Pult board;
- opens five Claude Code windows, each in its project, on Opus or Sonnet, with
  its first message already sent. `PROMPTS.md` lists the messages.

The prompts cover every status and three kinds of reference: a file inside the
project (`SPEC.md`), a file in another project (`../shop-site/SPEC.md`), and
several projects at once. Five parallel chats use your subscription.

## 2. During the test

- tg-bot asks you a question: answer it in its window (or leave it to see
  "Waiting for you").
- refactor: press Esc after about 20 seconds and close the window.

## 4. What to check

| Moment | Expected on the board |
|---|---|
| Chat is working | card in **Running now**, pulsing time |
| Chat asked you a question (prompt 2) | **Waiting for you** |
| Chat answered and stopped (1, 3, 5) | **Done, check it** after about 2.5 minutes of silence |
| You pressed Esc and closed the window (4) | **Interrupted** |
| Two chats in one folder (6) | two cards in the same project column |
| Press **Continue chat** on a card | the chat reopens in a new window with its history |
| Press the project name on a card | board jumps to that project column |
| Press **Open folder** | the folder opens in Explorer |
| **Summarise with AI** | a tinted AI-summary box with 2-4 bullets, last bullet is the next step |
| **Analyse with AI** on a project | four lines: what it is, open, last discussed, next step |
| **Translate cards** with EN/DE | everything on the board in one language |
| Period buttons / from-to dates | cards appear and disappear accordingly |

## 5. What to write down (for the next version)

- A card whose status is wrong: which one, and what the last message was.
- A chat that is missing, and the line at the bottom of the board that says
  how many chat files Pult found.
- Anything you wanted to click and could not.

Known gap: the card does not show which model (Opus or Sonnet) ran the chat.
It is easy to add, tell me if you want it.

## 6. Clean up

Delete the `pult-testbed` folder. The chats stay in your Claude Code history;
the board's own files are in `pult\cache`.
