# See your voice in the browser

```
mannerism ui
```

opens a page in your browser with your voice cards (each habit with its count and a real example from your own
writing), the rules learned from your edits (with a draft-versus-what-you-sent view), and a few insights: edits per
draft over time, how often you send a draft as written, the AI tells the lint caught, and words per message by
register. Where there is too little stored data it says "not enough data yet" instead of showing a number.

The page also has an **Is this you?** section. It lists groups of stored texts (the same source, account, project and
kind of session, or a message that repeats across many sessions) with counts, date range and a few masked examples,
groups with machine-typed texts held back first. For each group:

- **Not me** asks you to confirm ("Delete 17 stored messages from this group?"), then deletes those messages and
  everything learned from them, and skips the group from now on. This cannot be undone.
- **It's me** releases the group's held texts into your voice and remembers the answer, so later texts from the group are
  never held.

With nothing held, the section says so and offers to list all groups. The same two actions are in the terminal:
`mannerism exclude add --group ID` and `mannerism review keep ID`.

Everything shown is read from files stored on your machine. The page listens on 127.0.0.1 only (a random port, or
`--port`), checks the `Host` header, needs a token that is new on every launch, sends no CORS headers, and loads no
script, font or image from the network, so it works offline. It is read-only except for those two actions, which are
the only POST requests it accepts. Each one also needs the page's own session cookie, a second per-launch token sent in
a header, an `Origin` that is exactly the page's address and a JSON body, so another website open in your browser cannot
trigger them. The second token is given out once per launch, to the first tab that loads the page; a second tab shows the
groups but has no buttons (restart `mannerism ui` to review in another tab). A program on your computer that copies the
page's cookie could still use the buttons, so do not run `mannerism ui` on a shared machine. `--no-open` prints the address without opening a browser. After `mannerism forget`, reload the page to see
the data gone.
