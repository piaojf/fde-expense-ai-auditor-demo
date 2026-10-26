"""Small local image challenge; answers never leave the server as text."""

import base64
import io
import secrets

from PIL import Image, ImageDraw, ImageFont

CAPTCHA_TTL = 120
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def generate_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(4))


def render_captcha(code):
    image = Image.new("RGB", (160, 56), "#edf4ff")
    draw = ImageDraw.Draw(image)
    for _ in range(9):
        draw.line(
            [(secrets.randbelow(160), secrets.randbelow(56)) for _ in range(2)],
            fill=secrets.choice(("#c3d5ef", "#d2def0", "#aabfdd")), width=1,
        )
    font = ImageFont.load_default(size=30)
    for i, character in enumerate(code):
        glyph = Image.new("RGBA", (38, 48))
        ImageDraw.Draw(glyph).text((7, 5), character, font=font, fill="#173f7a")
        glyph = glyph.rotate(secrets.randbelow(29) - 14, resample=Image.Resampling.BICUBIC)
        image.paste(glyph, (i * 36 + 6, secrets.randbelow(5)), glyph)
    for _ in range(65):
        draw.point((secrets.randbelow(160), secrets.randbelow(56)), fill="#8da9cf")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
