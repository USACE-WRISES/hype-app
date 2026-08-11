"""The Conceptual Model report: the shipped figure, and the document that carries it.

WHY THIS FILE EXISTS. The figure used to be drawn in matplotlib from copy that lived in
`signature.py`, so three things were structural: the copy went through `validate_signature`'s
em-dash and ranking-word sweep, the four card titles came from `registry.FUNCTIONS` and could not
disagree with the report sections, and a missing asset failed the build. A hand-authored SVG is
better artwork and gives all three up. These tests take them back.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from hype_app import report, signature as sg
from hype_app.functions import registry as fn_reg

SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def svg_text() -> str:
    return report.CONCEPT_SVG.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def drawn_strings() -> list[str]:
    """Every string the figure puts in front of a reader, entities decoded.

    Parsed rather than regexed, so `&#8212;` cannot slip through as an em dash the way it would
    past a search for the literal character.

    `el.tail` IS SWEPT, not just `el.text`. Copy written after a `</tspan>` and before the closing
    `</text>` renders exactly like the rest and would otherwise be invisible to all three sweeps
    below. The cards are dense multi-line `<tspan>` blocks, which is where one would first appear.
    """
    root = ET.parse(report.CONCEPT_SVG).getroot()
    wanted = {SVG_NS + t for t in ("text", "tspan", "title", "desc")}
    return [s.strip() for el in root.iter() if el.tag in wanted
            for s in (el.text, el.tail) if s and s.strip()]


@pytest.fixture(scope="module")
def drawn_elements() -> list[str]:
    """One entry per `<text>` element, that element joined with its own `<tspan>`s, lowercased.

    Two things this does that `drawn_strings` cannot. It excludes `<title>`/`<desc>`, which are
    prose for a screen reader and name all four functions in a sentence, so a check run over them
    passes even when a card has been deleted. And it joins each element with its `<tspan>`s, so a
    label wrapped onto two lines still reads as the one phrase the registry spells."""
    root = ET.parse(report.CONCEPT_SVG).getroot()
    # joined on a SPACE, not "": the tspans are written back to back with no whitespace between
    # them, so "".join glues the last word of one line to the first word of the next.
    lines = [re.sub(r"\s+", " ", " ".join(el.itertext())).strip().lower()
             for el in root.iter(SVG_NS + "text")]
    return [x for x in lines if x]


@pytest.fixture(scope="module")
def drawn_flow(drawn_elements) -> str:
    """`drawn_elements` run together, for phrases that legitimately span two elements."""
    return " ".join(drawn_elements)


class TestTheShippedFigure:
    def test_both_assets_are_present(self):
        """`git archive HEAD` builds the desktop payload, so an asset that exists locally and was
        never committed ships as nothing at all. `report.py` also raises at import for this."""
        assert report.CONCEPT_SVG.is_file(), report.CONCEPT_SVG
        assert report.CONCEPT_PNG.is_file(), report.CONCEPT_PNG

    def test_the_svg_is_self_contained(self, svg_text):
        """The report is one file. An external `href` would render locally, off the developer's
        disk, and arrive at the reader as a blank rectangle.

        Checked as "no href that is not a data: or fragment ref" rather than "no http", because
        the XML doctype and the SVG namespace are both http URLs and neither is a fetch."""
        external = re.search(r'(?:xlink:)?href="(?!data:|#)', svg_text)
        assert not external, f"external reference at offset {external.start()}"
        assert "<script" not in svg_text

    def test_the_raster_matches_the_vector(self, svg_text):
        """The PDF places the PNG and the HTML places the SVG, so a PNG rebuilt from a different
        version of the figure would put two different documents in front of one reader. Aspect is
        the cheap tell, and it is what catches forgetting to re-run the build script."""
        from PIL import Image

        vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg_text)
        assert vb, "the SVG lost its viewBox"
        w, h = int(vb.group(1)), int(vb.group(2))
        png_w, png_h = Image.open(report.CONCEPT_PNG).size
        assert abs(png_w / png_h - w / h) < 0.01, \
            f"PNG is {png_w}x{png_h}, SVG viewBox is {w}x{h}. Re-run make_concept_assets.py."
        # and it is worth printing: 2x the canvas is about 380 ppi at the placed 6.33 inches
        assert png_w >= w * 2, f"raster is only {png_w / w:.1f}x the canvas"

    def test_the_hyporheic_overlay_is_intact(self, svg_text):
        """The two cross-sections are a painted base plus a VECTOR overlay: one zone polygon and
        several bidirectional flow arrows per panel, edited graphically with
        `notes/functional_screening_conceptual_figure/hz_overlay_editor.html`.

        That editor rewrites the block between the two marker comments and passes the rest of the
        file through, so a bad export or a hand-edit can empty the overlay while leaving a figure
        that still renders, still self-contained, and still passing every other check here. This
        is the only thing that would notice."""
        assert "<!-- HZ-OVERLAY-START" in svg_text and "<!-- HZ-OVERLAY-END -->" in svg_text, \
            "the overlay markers are gone, so the editor can no longer find the block"
        block = svg_text[svg_text.index("<!-- HZ-OVERLAY-START"):
                         svg_text.index("<!-- HZ-OVERLAY-END -->")]
        assert block.count('id="hzOverlay"') == 1

        for gid, panel in (("hzLocal", "localized"), ("hzWide", "widespread")):
            start = block.index(f'id="{gid}"')
            end = block.index("</g>", start)
            g = block[start:end]
            assert g.count('class="hz-zone"') == 1, f"{panel}: expected exactly one zone polygon"
            arrows = g.count('class="hz-flow"')
            assert arrows >= 3, f"{panel}: only {arrows} flow arrows"

        # every path carries the vertices the editor drags; `d` is generated from them
        pts = re.findall(r'<path class="hz-(?:zone|flow)" data-pts="([^"]+)"', block)
        assert len(pts) == block.count("<path "), "an overlay path lost its data-pts"
        assert all(len(p.split()) >= 3 for p in pts)
        # WHOLE UNITS. The editor snaps drags to integers and regenerates `d` from `data-pts` on
        # load, so a fractional vertex makes `d` come back a tenth of a unit different and the
        # editor's no-edit round trip stops being byte identical. It cost exactly that once.
        assert not any("." in p for p in pts), \
            "an overlay vertex is fractional; the editor round trip will not be byte identical"
        # both ends of every arrow are drawn, which is what makes the exchange read as two-way
        assert "marker-start: url(#flowTail)" in svg_text
        assert "marker-end: url(#flowHead)" in svg_text

    def test_every_overlay_path_matches_its_own_vertices(self, svg_text):
        """`data-pts` is the source of truth and `d` is generated from it, so the two must agree.

        Reimplemented here rather than imported, deliberately. The generator is JavaScript inside
        `hz_overlay_editor.html`, and this is the only independent check that it still does what it
        claims: a drifted editor, a hand-edited `d`, or a file saved by some other tool all show up
        here. Silent drift is the failure mode, because the figure still renders either way."""
        def fmt(v):
            s = f"{round(v, 1):.1f}".rstrip("0").rstrip(".")
            return "0" if s in ("-0", "") else s

        def smooth_closed(pts):
            m = len(pts)
            out = [f"M{fmt(pts[0][0])} {fmt(pts[0][1])}"]
            for i in range(m):
                p0, p1, p2, p3 = pts[(i - 1) % m], pts[i], pts[(i + 1) % m], pts[(i + 2) % m]
                c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
                c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
                out.append(f"C{fmt(c1[0])} {fmt(c1[1])} {fmt(c2[0])} {fmt(c2[1])} "
                           f"{fmt(p2[0])} {fmt(p2[1])}")
            return " ".join(out) + " Z"

        block = svg_text[svg_text.index("<!-- HZ-OVERLAY-START"):
                         svg_text.index("<!-- HZ-OVERLAY-END -->")]
        paths = re.findall(r'<path class="hz-(zone|flow)" data-pts="([^"]+)"\s*\n?\s*d="([^"]+)"/>',
                           block)
        assert len(paths) == block.count("<path "), "an overlay path is not in the expected shape"
        for kind, raw, d in paths:
            pts = [tuple(float(v) for v in p.split(",")) for p in raw.split()]
            if kind == "zone":
                want = smooth_closed(pts)
            else:
                (x0, y0), (xm, ym), (x1, y1) = pts
                want = (f"M{fmt(x0)} {fmt(y0)} Q{fmt(xm)} {fmt(ym)} {fmt(x1)} {fmt(y1)}")
            assert want == d, (f"hz-{kind} at {raw.split()[0]}: d does not follow from data-pts.\n"
                               f"  d    {d[:110]}\n  want {want[:110]}")


class TestTheCopyLint:
    """The sweep `validate_signature` used to run over this text, moved onto the artwork."""

    def test_no_drawn_string_uses_an_em_dash(self, drawn_strings):
        """Standing project rule. The draft carried four, and one of them was an entity."""
        bad = [t for t in drawn_strings if "—" in t]
        assert not bad, bad

    def test_no_drawn_string_ranks_the_site(self, drawn_strings):
        """§8.6 and §18.5 forbid a universal good/bad judgment. A framing figure is exactly where
        one would reappear, because it is the one place tempted to explain what a reader WANTS."""
        hits = []
        for text in drawn_strings:
            low = text.lower()
            for word in sg.BANNED_RANKING_WORDS:
                if word in low.split() or f" {word} " in f" {low} ":
                    hits.append((word, text))
        assert not hits, hits

    def test_the_figure_names_the_functions_the_registry_names(self, drawn_flow):
        """The card titles used to come from `registry.FUNCTIONS[...].display_label`, so the
        figure and the report sections could not disagree about what the four functions are.
        Hand-authored text can, and did: the draft said "Dissolved Contaminants" where the app
        says "Dissolved Pollutants".

        Against `drawn_flow`, so a title split across two `<text>` elements ("POLLUTANT" over
        "ATTENUATION") still matches whole. This used to fall back to matching the first word
        alone, over a corpus that included `<desc>` -- and `<desc>` lists all four labels in
        prose, so the check passed on the description whether or not the cards were there."""
        for key in fn_reg.FUNCTION_ORDER:
            label = fn_reg.FUNCTIONS[key].display_label.lower()
            assert label in drawn_flow, \
                f"{fn_reg.FUNCTIONS[key].display_label!r} is not drawn on the figure"

    def test_the_figure_names_the_metrics_the_registry_names(self, drawn_elements):
        """Each card names its three headline metrics, spelled exactly as the pane and the report
        spell them. That is the point of the card: a reader who has seen a number in the app can
        find where it comes from, and the two surfaces cannot drift into different names for it.

        Renaming a `PaneKpi` in the registry now fails HERE, which is the only warning that the
        artwork needs a matching edit -- nothing else reads the figure.

        Matched as a WHOLE element, not as a substring of the figure's running text. A substring
        check passes on an accident: rename this KPI to "Nitrate removed" and the nutrient card's
        own description ("Nitrate removed once the returning water goes anoxic") satisfies it while
        the metric row still says something else. One label is one `<text>`, so require that."""
        for key in fn_reg.FUNCTION_ORDER:
            fspec = fn_reg.FUNCTIONS[key]
            for kpi in fn_reg.PROCESSES[fspec.primary_process].kpis:
                assert kpi.label.lower() in drawn_elements, (
                    f"{fspec.display_label}: no text element on the figure reads {kpi.label!r}. "
                    f"Edit notes/functional_screening_conceptual_figure/iterations/ and re-run "
                    f"make_concept_assets.py --use N.")

    def test_the_lint_would_actually_catch_something(self, drawn_strings):
        """Otherwise the three sweeps above pass for the wrong reason the moment the parse stops
        finding text at all."""
        assert len(drawn_strings) > 40, len(drawn_strings)
        assert any("hyporheic" in t.lower() for t in drawn_strings)


class TestTheDocument:
    def test_the_html_embeds_the_figure_as_a_data_uri(self):
        import base64

        html = report.concept_html("Mink Brook")
        assert "data:image/svg+xml;base64," in html
        assert "Mink Brook" in html and report.CONCEPT_TITLE in html
        # the payload really is the shipped file, not a placeholder
        b64 = re.search(r'data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)', html).group(1)
        assert base64.b64decode(b64) == report.CONCEPT_SVG.read_bytes()

    def test_the_html_keeps_click_to_enlarge(self):
        """The lightbox handler filters on `tagName === "IMG"` AND `classList.contains("fig")`.
        An inline `<svg>` would look identical and silently lose the zoom, which is the only way
        to read a figure this dense in a scrolling document."""
        html = report.concept_html()
        assert 'class="fig"' in html
        assert 'id="figzoom"' in html
        assert 'contains("fig")' in html

    def test_the_document_carries_no_site_metadata(self):
        """It says nothing about a site, so a site header would be a promise it does not keep."""
        html = report.concept_html("Mink Brook")
        for chip in ("Analyst", "Discharge", "Reach:", "Volume basis"):
            assert chip not in html, chip

    def test_the_pdf_is_one_page(self):
        """THE FRAME IS SMALLER THAN THE MARGINS SUGGEST. `SimpleDocTemplate` builds a `Frame`
        with 6 pt of padding inside the 1 inch margins, so the usable box is 456 x 636 pt, not
        468 x 648. Both ways of getting this wrong are quiet: too tall raises `LayoutError`, and
        too tall by less flows the caption onto a page of its own while the image still fits."""
        pdf = report.concept_pdf_bytes("Mink Brook")
        assert pdf.startswith(b"%PDF-")
        assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 1

    def test_the_pdf_places_the_figure_and_its_caption_together(self):
        """Measured against a real frame, so the page count above cannot pass because reportlab
        silently dropped something."""
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Frame, Image, Paragraph, Spacer

        src = open("hype_app/report.py", encoding="utf-8").read()
        m = re.search(r"img\._restrictSize\((\d+\.?\d*) \* inch, (\d+\.?\d*) \* inch\)", src)
        assert m, "the figure's placement is no longer a _restrictSize"
        img = Image(str(report.CONCEPT_PNG))
        img._restrictSize(float(m.group(1)) * inch, float(m.group(2)) * inch)

        frame = Frame(inch, inch, letter[0] - 2 * inch, letter[1] - 2 * inch, id="normal")
        styles = getSampleStyleSheet()
        small = styles["BodyText"].clone("small", fontSize=8, leading=10)
        need = sum(f.wrap(frame._aW, 9999)[1] + getattr(f, "spaceBefore", 0)
                   + getattr(f, "spaceAfter", 0)
                   for f in (Paragraph(report.CONCEPT_TITLE, styles["Title"]),
                             Spacer(1, 0.10 * inch), img,
                             Paragraph(report.CONCEPT_CAPTION, small)))
        assert img.drawWidth <= frame._aW, (img.drawWidth, frame._aW)
        assert need <= frame._aH, (f"the page needs {need / inch:.2f} in, frame is "
                                   f"{frame._aH / inch:.2f} in")


class TestTheReportNode:
    def test_the_conceptual_model_is_the_first_report(self):
        from hype_app import ui_tree

        kids = [n["id"] for n in ui_tree.NODES if n.get("parent") == "report"]
        assert kids[0] == "report.concept", kids
        assert kids == ["report.concept", "report.hyd", "report.fn", "report.cmp"]

    def test_it_is_never_greyed_out(self):
        """`_push_tree_state` builds its disabled set from the non-None `NODE_STEP` entries, and
        this document needs no run behind it: gating it would put the only always-readable
        document behind the analysis it exists to explain."""
        from hype_app import ui_tree

        assert ui_tree.NODE_STEP["report.concept"] is None
        # ...and the group it hangs off has no prerequisite wall either
        src = open("app.py", encoding="utf-8").read()
        prereqs = src[src.index("    PREREQS = {"):src.index("for _fnid in (")]
        assert '"report":' not in prereqs, "the hub is walled off again"
        assert '"report.concept"' not in prereqs
        # the comparison manager is the same shape: reachable before any run, gated by words
        assert '"report.cmp"' not in prereqs
        assert 'for _rid in ("report.hyd", "report.fn")' in prereqs

    def test_opening_it_never_waits_on_a_report_build(self):
        """It shares nothing with `report_task`: no results, no staleness, no files on disk."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _open_built_report(doc)"):]
        body = body[:body.index("@reactive.effect")]
        head = body[:body.index('if report_task.status() == "running"')]
        assert 'if doc == "concept"' in head and "return" in head
