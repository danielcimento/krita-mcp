"""Valid layer blend modes: Krita's composite-op IDs (D17). Pure: no krita / PyQt5 imports.

``Node.setBlendingMode`` stores any string, invalid ones included, and a
layer with an unknown ID silently renders like Normal (spike Q15), so the
plugin checks IDs itself against this list.

Source: ``libs/pigment/KoCompositeOpRegistry.cpp`` (the ``m_map.insert``
calls in the ``KoCompositeOpRegistry`` constructor, the ops offered as
blending modes, by category) and the ID strings in
``libs/pigment/KoCompositeOpRegistry.h`` (``COMPOSITE_*`` constants), at
commit ``bd6e8d061d2f0388c5b10278786cf0fe62e3135f``: the commit the
installed Krita 5.3.4 reports (``5.3.4.1 (git bd6e8d0)``), tagged
``v6.0.4.1``. KDE has no ``v5.3.4`` tag; Krita 5.3 and 6.0 are built from
the same source.

    https://raw.githubusercontent.com/KDE/krita/bd6e8d061d2f0388c5b10278786cf0fe62e3135f/libs/pigment/KoCompositeOpRegistry.cpp
    https://raw.githubusercontent.com/KDE/krita/bd6e8d061d2f0388c5b10278786cf0fe62e3135f/libs/pigment/KoCompositeOpRegistry.h

138 IDs. The header defines 148 constants; the 10 left out are internal
ops never offered as a layer blending mode: ``clear``, ``colorize``,
``displace``, ``in``, ``minus``, ``nocomposition``, ``out``, ``plus``,
``pass through`` (a group's pass-through is a separate flag) and
``undefined``. Some IDs contain spaces (``"hard mix"``, ``"linear light"``):
they are Krita's spelling, kept as is. Matching is exact and
case-sensitive, as in Krita.
"""

import difflib

IDS = frozenset((
    # arithmetic
    "add", "subtract", "multiply", "divide", "inverse_subtract",
    # binary
    "xor", "or", "and", "nand", "nor", "xnor", "implication", "not_implication", "converse",
    "not_converse",
    # dark
    "burn", "linear_burn", "darken", "gamma_dark", "darker color", "shade_ifs_illusions",
    "fog_darken_ifs_illusions", "easy burn",
    # light
    "dodge", "dodge_hdr", "linear_dodge", "lighten", "linear light", "screen", "pin_light",
    "vivid_light", "vivid_light_hdr", "flat_light", "hard_light", "soft_light_ifs_illusions",
    "soft_light_pegtop_delphi", "soft_light", "soft_light_svg", "gamma_light",
    "gamma_illumination", "lighter color", "pnorm_a", "pnorm_b", "super_light",
    "tint_ifs_illusions", "fog_lighten_ifs_illusions", "easy dodge", "luminosity_sai",
    # modulo
    "modulo", "modulo_continuous", "divisive_modulo", "divisive_modulo_continuous",
    "modulo_shift", "modulo_shift_continuous",
    # negative
    "diff", "equivalence", "additive_subtractive", "exclusion", "arc_tangent", "negation",
    # mix
    "normal", "behind", "greater", "overlay", "lambert_lighting", "lambert_lighting_gamma2.2",
    "erase", "alphadarken", "marker", "hard mix", "hard_mix_hdr", "hard_mix_photoshop",
    "hard_mix_softer_photoshop", "grain_merge", "grain_extract", "parallel", "allanon",
    "geometric_mean", "destination-atop", "destination-in", "hard overlay", "hard_overlay_hdr",
    "interpolation", "interpolation 2x", "penumbra a", "penumbra b", "penumbra c", "penumbra d",
    # misc
    "bumpmap", "combine_normal", "dissolve", "copy_red", "copy_green", "copy_blue", "copy",
    "tangent_normalmap",
    # hsy
    "color", "hue", "tint", "saturation", "luminize", "dec_saturation", "inc_saturation",
    "dec_luminosity", "inc_luminosity",
    # hsi
    "color_hsi", "hue_hsi", "saturation_hsi", "intensity", "dec_saturation_hsi",
    "inc_saturation_hsi", "dec_intensity", "inc_intensity",
    # hsl
    "color_hsl", "hue_hsl", "saturation_hsl", "lightness", "dec_saturation_hsl",
    "inc_saturation_hsl", "dec_lightness", "inc_lightness",
    # hsv
    "color_hsv", "hue_hsv", "saturation_hsv", "value", "dec_saturation_hsv", "inc_saturation_hsv",
    "dec_value", "inc_value",
    # quadratic
    "reflect", "glow", "freeze", "heat", "glow_heat", "heat_glow", "reflect_freeze",
    "freeze_reflect", "heat_glow_freeze_reflect_hybrid",
))

DEFAULT = "normal"

# D17: the two IDs of Krita's additive blend; they render byte-identically.
ADDITIVE = frozenset({"add", "linear_dodge"})


def is_valid(mode):
    """True if ``mode`` is a Krita layer blend-mode ID."""
    return isinstance(mode, str) and mode in IDS


def suggest(mode, n=3):
    """Up to ``n`` valid IDs close to ``mode``, best first (for error hints)."""
    if not isinstance(mode, str):
        return []
    return difflib.get_close_matches(mode.lower(), sorted(IDS), n=n, cutoff=0.6)
