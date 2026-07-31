# The investor deck

```bash
pip install python-pptx
python presentation/make_deck.py     # -> Biolyt_Platform.pptx
```

61 slides. Figures are read from the repo — `graph/sources.py`,
`graph/emit.py`, `graph/lake_sample.json` — so regenerating after a data or
schema change updates the deck rather than leaving it stale.

| file | |
|---|---|
| `make_deck.py` | slide-by-slide narrative |
| `content.py` | pulls sources, columns, example rows, entities, edges |
| `theme.py` | palette, layout primitives, transitions and entrance motion |

**Colour carries meaning.** Blue is the graph, violet the document store,
cyan acquisition, green verified, amber a caveat. Keep that consistent when
editing.

**Motion is raw DrawingML.** python-pptx has no animation API, so transitions
and entrance effects are injected as XML. A malformed timing tree makes
PowerPoint show a *repair* prompt on open — in front of investors that is
worse than no animation. After changing `theme.animate` or
`theme.transition`, always re-validate:

```powershell
$ppt = New-Object -ComObject PowerPoint.Application
$p = $ppt.Presentations.Open("presentation\Biolyt_Platform.pptx", $true, $false, $false)
$p.Slides.Item(1).TimeLine.MainSequence.Count   # non-zero = effects registered
$p.Close(); $ppt.Quit()
```

**Titles wrap silently.** python-pptx cannot measure text, so `slide_title`
switches to a two-line layout above 50 characters. A longer title that stays
on one line will sit on top of the content beneath it.
