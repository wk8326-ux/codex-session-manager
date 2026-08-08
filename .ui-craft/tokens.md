# Local Project Console tokens

## Visual direction

- **Style:** restrained operational workbench
- **Signature:** a continuous process bus connecting every registered service node
- **Density:** medium-high on desktop, progressive disclosure on mobile

## Color

- Canvas: `#eef1ef`
- Raised surface: `#f8faf8`
- Primary ink: `#18221f`
- Secondary ink: `#68736f`
- Accent forest: `#2d715b`
- Running: `#27845a`
- Warning: `#ad6b1d`
- Danger: `#b44538`

Color is reserved for actions, focus, and semantic state. The base interface stays neutral.

## Typography

- Display: `Bahnschrift`, `Microsoft YaHei UI`, system sans-serif
- Body: `Microsoft YaHei UI`, `Segoe UI`, system sans-serif
- Utility/data: `Cascadia Mono`, `Consolas`, monospace
- Letter spacing remains `0` throughout.

## Shape and depth

- Inputs: `6px`
- Buttons: `6px`
- Service surface: `8px`
- Dialogs: `8px`
- Raised surfaces use a two-layer cool-tinted shadow.

## Motion

- Hover and pressed feedback: `120-160ms`
- Dialog entrance: native dialog behavior with a short opacity transition only
- All motion is removed when `prefers-reduced-motion: reduce` is active
