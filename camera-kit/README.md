# Lego D&D Table

Install (Windows or Mac, Python 3.10+):
    pip install opencv-contrib-python pygame numpy pillow

1. Print tags:      python lego_table.py --make-tags 0-15 --size-mm 16   (print tags.pdf at 100%)
2. Try it, no camera: python lego_table.py --simulate                    (0-9 pick a tag, click to place)
3. Calibrate:       python lego_table.py --calibrate minis               (tags 0-3 on the 4 red corner squares)
4. Play:            python lego_table.py

Edit config.json for grid size, player names, fog rooms and triggers.
Cells are [column, row] counted from the top-left square = [0, 0]. "area" is [x0, y0, x1, y1], inclusive.
Trigger actions: reveal, hide, show_layer, hide_layer, toggle_layer, sound, flash, message, enable, disable.
Put map art in maps/ (base.png plus transparent PNG overlays the same size) and sounds in sounds/.
Hotkeys: g grid/IDs, f fog, r reset, p light pads, q quit.
