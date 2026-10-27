import mailbox
from email.message import EmailMessage

from mannerism.adapters._util import sha
from mannerism.adapters.mbox import Mbox, html_to_text, strip_quotes_and_sigs

from .conftest import run


def mid_ref(mid: str) -> str:
    """The hashed source_ref a Message-ID of `mid` produces (P1-2/P2: the real Message-ID is never stored)."""
    return f"mid:{sha(mid)}"


def msg(frm, to, subject, body, mid, date="Mon, 02 Mar 2026 10:00:00 +0100", html=None, **hdr):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"], m["Date"] = frm, to, subject, f"<{mid}>", date
    for k, v in hdr.items():
        m[k.replace("_", "-")] = v
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    return m


def build(path):
    box = mailbox.mbox(str(path))
    box.add(
        msg(
            "Jo Dev <jo.dev@example.org>",
            "ana@example.org",
            "Re: plan",
            "Sounds good, call me on +44 20 7946 0958.\n\nOn Mon, 1 Mar 2026 at 09:00, Ana <ana@example.org> wrote:\n"
            "> earlier text\n> more\n\n-- \nJo\nJo Dev, Acme",
            "m2@example.org",
            References="<m0@example.org> <m1@example.org>",
            In_Reply_To="<m1@example.org>",
        )
    )
    box.add(msg("Ana <ana@example.org>", "jo.dev@example.org", "plan", "others text", "m1@example.org"))
    box.add(
        msg(
            "Jo Dev <JO.DEV@example.org>",
            "ana@example.org",
            "html",
            "",
            "m3@example.org",
            html="<html><style>p{}</style><body><p>Hello <b>Ana</b>,</p><p>see you at five</p>"
            "<p>Sent from my iPhone</p></body></html>",
        )
    )
    box.add(
        msg(
            "Jo Dev <jo.dev@example.org>",
            "list@example.org",
            "newsletter",
            "bulk text here",
            "m4@example.org",
            Precedence="bulk",
        )
    )
    box.add(
        msg(
            "Jo Dev <jo.dev@example.org>",
            "a@example.org",
            "auto",
            "out of office until monday",
            "m5@example.org",
            Auto_Submitted="auto-replied",
        )
    )
    box.add(
        msg(
            "Jo Dev <jo.dev@example.org>",
            "a@example.org",
            "de",
            "Danke dir, passt.\n\nAm 01.03.2026 um 09:00 schrieb Ana:\n> alt",
            "m6@example.org",
        )
    )
    box.add(
        msg("Jo Dev <jo.dev@example.org>", "a@example.org", "nodate", "no date here", "m7@example.org", date="garbage")
    )
    box.flush()
    box.close()


def test_mbox_own_mail(home, me, tmp_path):
    p = tmp_path / "Sent.mbox"
    build(p)
    src = Mbox()
    locs = src.discover(me, {"paths": [str(p)]})
    assert [lc.path for lc in locs] == [p]
    units, cur = run(src, locs[0], None, me)
    got = {u.source_ref: u for u in units}
    assert set(got) == {mid_ref("m2@example.org"), mid_ref("m3@example.org"), mid_ref("m6@example.org")}
    assert "m2@example.org" not in str(got)  # the real Message-ID itself is never stored (P1-2/P2)
    assert got[mid_ref("m2@example.org")].text == "Sounds good, call me on [phone]."
    assert got[mid_ref("m3@example.org")].text == "Hello Ana,\n\nsee you at five"
    assert "iPhone" not in got[mid_ref("m3@example.org")].text
    assert got[mid_ref("m6@example.org")].text == "Danke dir, passt."
    u = got[mid_ref("m2@example.org")]
    assert (u.kind, u.register, u.audience) == ("email", "?", "unknown")
    assert u.thread_id and "m0" not in u.thread_id and u.audience_ref and "ana" not in u.audience_ref
    assert cur["skipped"] == 1  # the bad Date
    assert run(src, locs[0], cur, me)[0] == []


def test_mbox_gmail_labels_and_spoofing(home, me, tmp_path):
    """P1-2: a Gmail Takeout "All Mail" export carries every message once, so `From` alone is not enough --
    it must also be labelled Sent and never Spam/Trash, and an SPF/DMARC fail (anywhere, Gmail or not)
    means the From header was forged and the message is not really the user's own."""
    p = tmp_path / "AllMail.mbox"
    box = mailbox.mbox(str(p))
    box.add(msg("Jo Dev <jo.dev@example.org>", "a@example.org", "sent one", "a message i actually sent",
                "g1@example.org", X_Gmail_Labels="Sent,Important"))
    box.add(msg("Jo Dev <jo.dev@example.org>", "a@example.org", "inbox copy",
                "a message that only landed in the inbox", "g2@example.org", X_Gmail_Labels="Inbox"))
    box.add(msg("Jo Dev <jo.dev@example.org>", "a@example.org", "spam",
                "a spoofed message gmail filed as spam anyway", "g3@example.org", X_Gmail_Labels="Sent,Spam"))
    box.add(msg("Jo Dev <jo.dev@example.org>", "a@example.org", "spoofed", "someone else sent this pretending to be me",
                "g4@example.org", Authentication_Results="mx.google.com; spf=fail smtp.mailfrom=evil.example"))
    box.flush()
    box.close()
    src = Mbox()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    units, _ = run(src, loc, None, me)
    assert {u.source_ref for u in units} == {mid_ref("g1@example.org")}


def test_mbox_requires_emails(home, tmp_path):
    from mannerism.core import Me

    p = tmp_path / "x.mbox"
    build(p)
    src = Mbox()
    assert list(src.read(src.discover(Me(), {"paths": [str(p)]})[0], None, Me())[0]) == []


def test_mbox_helpers():
    assert strip_quotes_and_sigs("hi\n\n-----Original Message-----\nFrom: x\nolder") == "hi"
    assert (
        strip_quotes_and_sigs("hi there\n\nOn Tue, Mar 3, 2026 at 5:00 PM Ana\n<ana@example.org> wrote:\n> q")
        == "hi there"
    )
    assert strip_quotes_and_sigs("yo\n\nLe 3 mars 2026 à 17:00, Ana a écrit :\n> q") == "yo"
    assert strip_quotes_and_sigs("yo\n\nEl mar, 3 mar 2026, Ana escribió:\n> q") == "yo"
    assert strip_quotes_and_sigs("yo\n\nOp 3 mrt. 2026 schreef Ana:\n> q") == "yo"
    assert strip_quotes_and_sigs("answer\nFrom: Ana\nSent: Monday\nTo: Jo\nSubject: x\nold") == "answer"
    assert strip_quotes_and_sigs("keep\n> drop\nkeep too") == "keep\nkeep too"
    assert html_to_text("<p>a</p><script>x</script><p>b&amp;c</p>") == "a\n\nb&c"
