"""A window that is definitely moving, to test a signal that says nothing is.

Three honest zeros in a row -- a death screen, a paused menu, an unfocused
client whose every frame was a duplicate -- and a signal wired to return zero
unconditionally would have produced exactly the same three. The only way to
tell those apart is a surface known to change, owned by this test, so that
nothing belonging to anyone else has to be touched to prove it.
"""
import pathlib, sys, tkinter as tk

root = tk.Tk()
root.title("GameLens churn control")
root.geometry("480x300+120+120")
canvas = tk.Canvas(root, width=480, height=300, highlightthickness=0, bg="black")
canvas.pack()
box = canvas.create_rectangle(0, 0, 160, 300, fill="white", outline="")
state = {"x": 0, "dx": 24}


def step():
    state["x"] += state["dx"]
    if not 0 <= state["x"] <= 320:
        state["dx"] = -state["dx"]
    canvas.coords(box, state["x"], 0, state["x"] + 160, 300)
    root.after(33, step)


root.update_idletasks()
hwnd = int(root.frame(), 16) if False else root.winfo_id()
pathlib.Path(sys.argv[1]).write_text(str(hwnd))
print(f"hwnd {hwnd}", flush=True)
step()
root.mainloop()
