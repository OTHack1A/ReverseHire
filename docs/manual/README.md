# How ReverseHire works (LaTeX manual)

Rebuild: from this folder run `pdflatex -interaction=nonstopmode reversehire_manual.tex` three times (TOC, links and footer marks need it), check the `.log` has no line starting with `!`, then delete the build files (`.aux`, `.log`, `.toc`, `.out`). On Windows with MiKTeX: `"C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe" -interaction=nonstopmode reversehire_manual.tex`.
Sources: `reversehire_manual.tex` (layout and cover) plus one `chNN_name.tex` per chapter (18 files: chapters 1 to 16, then the unnumbered glossary and cheat sheet); the cover uses `../images/banner.png`.
Rule: whenever a feature changes, update the matching chapter in the same change (each file names the modules it describes in its first lines), and keep the cheat sheet on one page.
The cover shows the program version: when `src/reversehire/_version.py` changes, update the `Version` row of the cover in `reversehire_manual.tex`.
