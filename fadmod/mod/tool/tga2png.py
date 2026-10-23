
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinterdnd2 import TkinterDnD, DND_FILES
from PIL import Image


def convert_path(path):
    source = Path(path)

    if source.is_dir():
        # Find all TGA files, including those in subfolders
        files = [
            f for f in source.rglob("*")
            if f.is_file() and f.suffix.lower() == ".tga"
            and "PNG_Converted" not in f.parts
        ]
        output = source / "PNG_Converted"

    elif source.is_file() and source.suffix.lower() == ".tga":
        files = [source]
        output = source.parent / "PNG_Converted"

    else:
        return 0, 0

    output.mkdir(parents=True, exist_ok=True)
    converted = 0
    failed = 0

    for file in files:
        try:
            if source.is_dir():
                relative = file.relative_to(source)
            else:
                relative = Path(file.name)

            destination = output / relative.with_suffix(".png")
            destination.parent.mkdir(parents=True, exist_ok=True)

            with Image.open(file) as image:
                image.save(destination, "PNG")

            converted += 1

        except Exception as error:
            print(f"Failed: {file} - {error}")
            failed += 1

    return converted, failed


def drop_files(event):
    paths = root.tk.splitlist(event.data)

    status.config(text="Converting files...")
    root.update_idletasks()

    total_converted = 0
    total_failed = 0

    for path in paths:
        converted, failed = convert_path(path)
        total_converted += converted
        total_failed += failed

    status.config(
        text=f"Done! {total_converted} converted, {total_failed} failed."
    )

    messagebox.showinfo(
        "Conversion complete",
        f"Converted: {total_converted}\n"
        f"Failed: {total_failed}\n\n"
        "Your PNG files are in the PNG_Converted folder."
    )


# Create the app window
root = TkinterDnD.Tk()
root.title("TGA to PNG Converter")
root.geometry("500x320")
root.resizable(False, False)

title = tk.Label(
    root,
    text="TGA  →  PNG",
    font=("Arial", 25, "bold")
)
title.pack(pady=(25, 8))

description = tk.Label(
    root,
    text="Convert your TGA textures in seconds",
    font=("Arial", 11)
)
description.pack()

drop_area = tk.Label(
    root,
    text="DRAG & DROP\n\nA folder or TGA files here",
    font=("Arial", 13),
    relief="groove",
    borderwidth=2,
    bg="#eeeeee",
    width=40,
    height=6
)
drop_area.pack(padx=25, pady=18, fill="both")

drop_area.drop_target_register(DND_FILES)
drop_area.dnd_bind("<<Drop>>", drop_files)

status = tk.Label(
    root,
    text="Ready to convert",
    font=("Arial", 10)
)
status.pack(pady=4)

root.mainloop()
