# Gerber2Step

A lightweight desktop utility that converts KiCad Gerber and Excellon drill files into 3D-printable **STEP** models with recessed copper-tape trenches, through-holes, and optional positive press stamps.

## Background & Motivation

This tool was built to automate the chemical-free 3D-printed PCB fabrication method demonstrated by Freche Typen in [this video](https://www.youtube.com/watch?v=rPsZJydHG7M) (applying adhesive copper tape over a 3D-printed substrate with recessed trace channels and sanding the raised surface to isolate the traces).

I was iterating frequently on prototype boards and got tired of manually importing sketches and extruding trenches in CAD every time I changed a trace. **Gerber2Step** turns that repetitive CAD workflow into a one-click export straight from KiCad Gerbers to native `.step` files.

> **AI / Code Disclaimer:** While I have experience with Python, I do not have prior experience with the 3D/CAD libraries used under the hood (`build123d`, `shapely`, `gerbonara`). This application was built with AI assistance ("vibe coded") to solve a practical prototyping bottleneck. It works reliably for my workflow, and PRs, bug reports, or improvements from people who know these libraries well are very welcome!

> **Safety Disclaimer (Sanding Copper & 3D-Printed Parts):**
> Although this method avoids chemical etchants, **sanding copper tape and 3D-printed plastics (PLA, PETG, or resin) generates fine metallic and microplastic dust** that can irritate your lungs, eyes, and skin.
> * **Wet sand whenever possible:** Wetting the sandpaper traps most copper and plastic particles in a slurry, preventing them from becoming airborne (and keeping the plastic from melting).
> * **Wear PPE:** Wear a particulate respirator (**N95/P2 or P100**)—especially if dry-sanding, using power tools, or sanding cured resin/fiber-filled prints—along with eye protection and nitrile gloves (or wash your hands thoroughly with soap and water afterward).
> * **Clean up properly:** Wipe up the wet sanding slurry with a damp paper towel and dispose of it in the trash. Avoid blowing dust away with compressed air, letting slurry dry on your workspace, or rinsing copper/microplastics down the sink.

---

## Features

* **Direct KiCad Gerber & Drill Ingestion:** Automatically detects `*Edge_Cuts.gbr`, `*F_Cu.gbr`, `*B_Cu.gbr`, and `*.drl` files in a selected folder (gracefully skips missing optional layers for single-sided boards).
* **Native STEP Output:** Generates true B-Rep `.step` files via OpenCASCADE (`build123d`) rather than triangulated STL meshes, making them easy to edit in CAD or slice cleanly.
* **Single- & Double-Sided Support:** Independent trench depth controls for top (`F.Cu`) and bottom (`B.Cu`) layers (e.g., `0.2 mm` top, `0.3 mm` bottom to accommodate FDM bridging).
* **FDM Compensation:** Configurable through-hole diameter tolerance to counteract 3D-printed hole shrinkage, plus configurable arc resolution to balance smoothness and file size.
* **Positive Press Stamps:** Optionally generates mirrored top and bottom positive stamps to press copper tape firmly into the trenches, with configurable base thickness, extrusion height, XY clearance, and an option to exclude through-holes.
* **Saved Defaults:** Stores your preferred folders and parameters in a local `config.txt` file.

---

## Recommended Print & PCB Design Settings

For my prints, I used a **0.4 mm nozzle** at a **0.1 mm layer height**, with **ironing enabled on top surfaces** for the PCB (but disabled for the press stamps). Make sure your filament is dry—eliminating stringing is critical for keeping the trenches clean and isolated. In KiCad (or your preferred PCB design software), I used a minimum clearance of **0.4 mm** between traces and pads, **1.1 mm** PTH holes, **1.2 mm** trace widths on the bottom layer (printed facing up), and **1.5 mm** trace widths on the top layer (since the front copper plane was printed directly on the print bed). Using a smaller-diameter nozzle should allow you to make these features even slimmer.

---

## Download (Windows `.exe`)

If you don't want to install Python, grab the latest standalone Windows executable from the [**Releases**](https://github.com/T-S-Colombari/Gerber2Step/releases) page.

* **Note on Windows SmartScreen:** Because the executable is not code-signed with a paid certificate, Windows may show a *"Windows protected your PC"* warning on first launch. Click **More info → Run anyway** to open the app.

---

## How to Use

1. **Export from KiCad:**
   * In the PCB Editor, go to **File → Plot...** and plot `Edge.Cuts`, `F.Cu`, and (optionally) `B.Cu` to a folder.
   * Click **Generate Drill Files...** in the Plot dialog, choose **Excellon** format in **Millimeters** (Decimal format), and generate the `.drl` file into the same folder.
2. **Generate STEP Files:**
   * Open `Gerber2Step`, select your Gerber folder and output folder, and adjust your board thickness, trench depths, and stamp settings.
   * Click **Save as Default** to remember your settings in `config.txt`.
   * Click **Generate STEP**.

---

## Packaging

The standalone Windows `.exe` was packaged using **PyInstaller** with the following command:

```powershell
pyinstaller --noconsole --onefile --clean --name Gerber2Step --collect-all OCP --collect-all build123d --collect-all lib3mf --collect-all gerbonara gerber2step.py
