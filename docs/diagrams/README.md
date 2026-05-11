# Diagram style guide

Canonical visual standard for any diagram produced for Neural Bridge content (LinkedIn articles, blog posts, internal docs, social cards, slide decks). Established 2026-05-11.

## The standard

**Editorial technical-illustration aesthetic.** Think mid-twentieth-century architectural plates, modern engineering textbook figures, or scientific reference diagrams. Not flat infographic design. Not corporate slide-deck aesthetic.

Reference: `agent-memory-pyramid.png` in this directory is the canonical example. Future diagrams should evoke the same visual character.

### What "in style" looks like

- **Light dimensionality.** A 3/4 perspective or subtle isometric angle where appropriate. Not full 3D rendering, but the diagram should have some physical presence, not read as pure 2D flat-design.
- **Muted segmented color palette.** Earth tones, dusty blues, warm ambers, slate grays, gray-greens. Each component differentiated by color but the palette overall sits well below neon saturation.
- **Architectural / blueprint character.** Thin precise line work. Subtle annotation lines pointing at labeled regions. Number badges. Side labels showing lifetime, scope, or other axes. Bullet lists or short descriptors inside each component.
- **Editorial typography.** Modern technical sans-serif (Inter, IBM Plex Sans, or similar). Hierarchical weight: bold for component names, regular for descriptions, monospace for metadata labels (timestamps, dimensions, lifetimes).
- **Subtle texture.** Optional faint blueprint-paper texture in the background at very low opacity. Light cream or off-white base, not pure stark white.

### What "out of style" looks like (avoid)

- Pure flat 2D infographic with bright saturated colors
- AI cliche imagery: brains, neural networks, glowing nodes, robot heads
- 3D rendered glossy spheres / cubes / abstract shapes
- Generic web illustration (the Notion / Linear / Stripe corporate look is wrong for this brand)
- Drop shadows, blurs, soft gradients, glow effects
- Icons replacing component labels (icons can supplement but should never substitute for clear typography)
- Marketing flourishes, abstract organic shapes, ribbons or banners

## Why this style

- Reads as a serious technical contribution, not a marketing slide
- Survives the LinkedIn-feed thumbnail test (legible at mobile thumbnail size; doesn't get lost in a sea of bright infographic noise)
- Matches the build-in-public voice already established in writing (direct, specific, restrained)
- Hard to mimic with default AI image-generation defaults, which keeps the visual identity distinctive

## When to apply this guide

Any agent (or me) generating a diagram for Andy's content surfaces:

- LinkedIn article embeds
- neural-bridge.dev blog post figures
- Social-card thumbnails
- Internal architecture diagrams in this repo
- Pitch deck or research-paper figures (if those surface later)

When prompting nanobanana, ChatGPT image generation, Midjourney, or any other model: reference this style guide explicitly and adapt the prompt to enforce the constraints above. The pyramid diagram's prompt (drafted 2026-05-11) is a good starting template.

## Iteration discipline

Generate first with the prompt constraints above. If output is acceptable, ship it. If output drifts off-style:

1. Re-prompt with the specific anti-pattern called out ("no AI cliches", "no flat infographic style", etc.)
2. Add a positive reference ("editorial textbook plate style")
3. If still off, switch tools (nanobanana → Midjourney with style references → manual cleanup in design tooling)

Do not accept off-style output just because the model produced it. The visual identity compounds; one off-style image now costs disproportionate cleanup later.

## Inventory

| File | Diagram | Article / context |
|---|---|---|
| `agent-memory-pyramid.jpeg` | The seven layers of agent memory, pyramid form, generated via nanobanana 2026-05-11. Architectural blueprint aesthetic, muted segmented palette, 3/4 perspective. | LinkedIn draft `2026-05-11-three-kinds-of-agent-memory.md` |
