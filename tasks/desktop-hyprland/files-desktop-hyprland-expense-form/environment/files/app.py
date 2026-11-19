import json
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

window = Gtk.Window(title="Expense Form")
window.set_default_size(420, 260)
window.set_border_width(16)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
amount = Gtk.Entry()
amount.get_accessible().set_name("Amount")
memo = Gtk.Entry()
memo.get_accessible().set_name("Description")
submit = Gtk.Button(label="Submit expense")
submit.get_accessible().set_name("Submit expense")
status = Gtk.Label(label="No expense submitted")
status.get_accessible().set_name("Submission status")


def save(_button):
    path = Path("submitted.json")
    expenses = json.loads(path.read_text()) if path.exists() else []
    expenses.append({"amount": amount.get_text(), "memo": memo.get_text()})
    path.write_text(json.dumps(expenses) + "\n")
    status.set_text(f"Submitted expense {len(expenses)}: {amount.get_text()} — {memo.get_text()}")


submit.connect("clicked", save)
for widget in (Gtk.Label(label="Amount"), amount, Gtk.Label(label="Description"), memo, submit, status):
    box.add(widget)
window.add(box)
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()
