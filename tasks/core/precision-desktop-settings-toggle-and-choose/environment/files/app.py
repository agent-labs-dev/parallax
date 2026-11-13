import json
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

path = Path("preferences.json")
settings = json.loads(path.read_text())
window = Gtk.Window(title="Preferences")
window.set_default_size(420, 280)
window.set_border_width(16)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
notifications = Gtk.CheckButton(label="Desktop notifications")
notifications.get_accessible().set_name("Desktop notifications")
notifications.set_active(settings["notifications"])
light = Gtk.RadioButton.new_with_label_from_widget(None, "Light")
dark = Gtk.RadioButton.new_with_label_from_widget(light, "Dark")
light.get_accessible().set_name("Light theme")
dark.get_accessible().set_name("Dark theme")
(light if settings["theme"] == "Light" else dark).set_active(True)
login = Gtk.CheckButton(label="Launch on login")
login.get_accessible().set_name("Launch on login")
login.set_active(settings["launch_on_login"])
save = Gtk.Button(label="Save preferences")
save.get_accessible().set_name("Save preferences")
status = Gtk.Label(label="No unsaved changes")
status.get_accessible().set_name("Save status")


def persist(_button):
    path.write_text(
        json.dumps(
            {
                "notifications": notifications.get_active(),
                "theme": "Dark" if dark.get_active() else "Light",
                "launch_on_login": login.get_active(),
            }
        )
        + "\n"
    )
    status.set_text("Preferences saved")


save.connect("clicked", persist)
for widget in (notifications, Gtk.Label(label="Colour theme"), light, dark, login, save, status):
    box.add(widget)
window.add(box)
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()
