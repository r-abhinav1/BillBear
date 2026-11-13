# Fonts for the PDF

The PDF of the split is drawn with the PDF's built-in Helvetica, which only has
Western European letters. These fonts print the rest: dish, people and charge
names in Indian scripts, Sinhala, Thai, and Chinese, Japanese and Korean.
`utils/pdf_text.py` decides which text uses which file; English text and
amounts stay in Helvetica.

| File | Script | Licence |
|---|---|---|
| NotoSans{Devanagari,Bengali,Gurmukhi,Gujarati,Oriya,Tamil,Telugu,Kannada,Malayalam,Sinhala,Thai}-Regular.ttf | One script each | SIL Open Font License 1.1, see LICENSE-Noto.txt |
| DroidSansFallbackFull.ttf | Chinese, Japanese, Korean | Apache License 2.0, see LICENSE-Droid.txt |

They must be TrueType (glyf) fonts: ReportLab can't embed CFF-based OpenType
fonts such as Noto Sans CJK.
