import json
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

window = Gtk.Window(title="Records")
window.set_default_size(460, 360)
window.set_border_width(16)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
name = Gtk.Entry()
name.get_accessible().set_name("Collection name")
name.set_text(json.loads(Path("collection.json").read_text())["name"])
save = Gtk.Button(label="Save name")
save.get_accessible().set_name("Save name")
reset = Gtk.Button(label="Delete all records…")
reset.get_accessible().set_name("Delete all records")
listing = Gtk.Label(label="\n".join(row["title"] for row in json.loads(Path("records.json").read_text())))
listing.get_accessible().set_name("Collection records")
status = Gtk.Label(label="3 records")
status.get_accessible().set_name("Collection status")


def rename(_button):
    Path("collection.json").write_text(json.dumps({"name": name.get_text()}) + "\n")
    status.set_text(f"Collection renamed to {name.get_text()}")


def confirm_delete(*_args):
    dialog = Gtk.MessageDialog(
        transient_for=window,
        modal=True,
        message_type=Gtk.MessageType.WARNING,
        buttons=Gtk.ButtonsType.NONE,
        text="Delete all records?",
    )
    dialog.set_title("Delete all records?")
    dialog.format_secondary_text("This permanently removes every entry in this collection.")
    cancel = dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    cancel.get_accessible().set_name("Cancel")
    delete = dialog.add_button("Delete all records", Gtk.ResponseType.ACCEPT)
    delete.get_accessible().set_name("Confirm delete all records")
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    if dialog.run() == Gtk.ResponseType.ACCEPT:
        count = len(json.loads(Path("records.json").read_text()))
        path = Path("deletions.json")
        path.write_text(str(json.loads(path.read_text()) + count) + "\n")
        Path("records.json").write_text("[]\n")
        listing.set_text("No records")
        status.set_text("All records deleted")
    dialog.destroy()
    return False


save.connect("clicked", rename)
reset.connect("clicked", confirm_delete)
for widget in (Gtk.Label(label="Collection name"), name, save, listing, reset, status):
    box.add(widget)
window.add(box)
window.connect("destroy", Gtk.main_quit)
window.show_all()
# A confirmation left open when the user hands over the desktop.
GLib.idle_add(confirm_delete)
Gtk.main()
