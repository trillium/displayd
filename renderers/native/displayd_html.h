/*
 * displayd html renderer -- C ABI between the PIL painter (Python, via
 * ctypes) and the litehtml layout engine.
 *
 * Division of labour: litehtml parses and lays out, the embedder paints.
 * Nothing but plain C data crosses this boundary -- no C++ objects, no
 * std::string, no exceptions -- so a caller cannot corrupt the heap by
 * mis-declaring a struct, and a litehtml throw cannot unwind into Python.
 * Every callback is optional; a NULL one is simply "do not draw it".
 *
 * Build with tools/build_litehtml.sh (pinned upstream commit, no network
 * at render time). Licence of the layout engine and its bundled parser:
 * LICENSE-litehtml (BSD-3-Clause) and LICENSE-gumbo (Apache-2.0).
 */
#ifndef DISPLAYD_HTML_H
#define DISPLAYD_HTML_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The library is compiled with -fvisibility=hidden so only this ABI is
 * exported; the build links litehtml's objects straight in, and none of that
 * belongs in the host process's symbol table. */
#if defined(__GNUC__) || defined(__clang__)
#define LHTML_API __attribute__((visibility("default")))
#else
#define LHTML_API
#endif

/* ---------------------------------------------------------------- types */

typedef struct lhtml_color
{
    uint8_t r, g, b, a;
} lhtml_color;

typedef struct lhtml_rect
{
    double x, y, w, h; /* w or h <= 0 means "no clip" */
} lhtml_rect;

typedef struct lhtml_border_side
{
    double    width; /* px */
    lhtml_color color;
    int       style; /* lhtml_border_style */
} lhtml_border_side;

/* Per-corner border radii, resolved by litehtml to border-box pixels.
 *
 * CSS radii are eight independent values, not one number: each corner has
 * its own horizontal and vertical radius (the `60px 10px / 20px 40px` slash
 * form, and the `border-top-left-radius` longhands, both land here). A
 * single collapsed radius silently rounds the wrong corners, so all eight
 * travel together. Zero means "square at this corner"; an all-zero struct
 * and a NULL pointer both mean the element declared no radius at all, and
 * the embedder must draw the plain square rectangle for either. */
typedef struct lhtml_radii
{
    double top_left_x,     top_left_y;
    double top_right_x,    top_right_y;
    double bottom_right_x, bottom_right_y;
    double bottom_left_x,  bottom_left_y;
} lhtml_radii;

/* Sides are ordered left, top, right, bottom (matches litehtml). */
#define LHTML_SIDE_LEFT   0
#define LHTML_SIDE_TOP    1
#define LHTML_SIDE_RIGHT  2
#define LHTML_SIDE_BOTTOM 3

/* Font style / weight / decoration bits, mirrored so the embedder does not
 * have to include any litehtml header. */
#define LHTML_FONT_ITALIC    1
#define LHTML_FONT_UNDERLINE 2
#define LHTML_FONT_OVERLINE  4
#define LHTML_FONT_LINE_THROUGH 8

typedef struct lhtml_font_desc
{
    const char *family; /* raw CSS font-family list, first entry preferred */
    double      size;   /* px */
    int         style;  /* LHTML_FONT_* bits */
    int         weight; /* 100..900 */
    int         decoration;             /* LHTML_FONT_* text bits */
    double      decoration_thickness;   /* px */
    lhtml_color decoration_color;
} lhtml_font_desc;

/* Filled in by the create_font callback. Litehtml needs these to resolve
 * em/ex/ch units and line boxes, so they must be real measurements, not
 * guesses -- the reference test container returning zeros is exactly the
 * bug that makes text vanish. */
typedef struct lhtml_font_metrics
{
    uintptr_t id; /* out: embedder font handle, returned to every callback */
    double    height;
    double    ascent;
    double    descent;
    double    x_height;
    double    ch_width;
} lhtml_font_metrics;

/* background-repeat values we act on; anything else draws nothing. */
#define LHTML_REPEAT_NO_REPEAT 0
#define LHTML_REPEAT_REPEAT_X   1
#define LHTML_REPEAT_REPEAT_Y   2
#define LHTML_REPEAT_REPEAT     3

/* ------------------------------------------------------------ callbacks */

typedef struct lhtml_callbacks
{
    void *ctx; /* opaque, passed back untouched; may be NULL */

    /* Font lifetime. create_font MUST write *out; delete_font must release
     * the handle it was given. litehtml calls delete_font for every font it
     * created when the document dies, so honouring it is what keeps
     * steady-state memory flat. */
    void (*create_font)(void *ctx, const lhtml_font_desc *descr,
                        lhtml_font_metrics *out);
    void (*delete_font)(void *ctx, uintptr_t font_id);
    double (*text_width)(void *ctx, uintptr_t font_id, const char *utf8);

    /* Painting. box is in canvas pixels; clip is the current clip stack top
     * (w<=0 or h<=0 means unclipped).
     *
     * radii may be NULL, and is then the same as all-zero: draw the square
     * rectangle, exactly as an embedder written before this field existed
     * would. It is a pointer rather than a trailing value so the two
     * callbacks keep their old shape at the source level. */
    void (*draw_fill)(void *ctx, lhtml_rect box, lhtml_rect clip,
                      lhtml_color color, const lhtml_radii *radii);
    void (*draw_borders)(void *ctx, lhtml_rect box, lhtml_rect clip,
                         const lhtml_border_side sides[4],
                         const lhtml_radii *radii);
    void (*draw_text)(void *ctx, uintptr_t font_id, const char *utf8,
                      lhtml_color color, lhtml_rect box, lhtml_rect clip);
    /* Backgrounds. Three distinct rectangles, and conflating any two of them
     * is the bug that makes a tiled background draw exactly one tile:
     *   box   -- the FIRST TILE: origin_box, i.e. background-position offset
     *            plus the size background-size resolved to (natural size for
     *            `auto`). A repeat mode is meaningless without this.
     *   area  -- border_box: the region the repeat must FILL.
     *   clip  -- current clip stack top, already intersected with the
     *            background's own clip-box. w<=0 or h<=0 means unclipped. */
    void (*draw_image)(void *ctx, const char *url, const char *base_url,
                       lhtml_rect box, lhtml_rect area, lhtml_rect clip,
                       int repeat);

    /* Image lookup, called during layout for <img> sizing. Returning 0 for
     * both axes makes the engine treat the image as absent. */
    void (*image_size)(void *ctx, const char *url, const char *base_url,
                       double *width, double *height);
} lhtml_callbacks;

/* ------------------------------------------------------------------- api */

#define LHTML_OK              0
#define LHTML_ERR_MISSING     1 /* empty input */
#define LHTML_ERR_PARSE       2 /* litehtml threw while parsing/layout */
#define LHTML_ERR_CALLBACK    3 /* the embedder reported a paint failure */
#define LHTML_ERR_INTERNAL    4 /* anything else, message in err */

#define LHTML_ERRLEN 240

/* Pinned upstream revision plus compiler, for /state and bug reports. */
LHTML_API const char *lhtml_version(void);

/*
 * Parse + lay out + draw `html` at `width` x `height` into the embedder via
 * `cb`. Synchronous and reentrant per thread: nothing is cached between
 * calls, so a caller may render as often as it likes.
 *
 * On success writes the laid-out content height (>= height when the document
 * overflows) to *out_content_height and returns LHTML_OK. On failure returns
 * one of the LHTML_ERR_* codes and, when err is non-NULL, a NUL-terminated
 * human-readable reason. A partial document may already have been painted
 * when the failure happens during draw; the embedder should discard it.
 */
LHTML_API int lhtml_render(const lhtml_callbacks *cb, const char *html, int width,
                           int height, double *out_content_height, char *err,
                           int errlen);

#ifdef __cplusplus
}
#endif

#endif /* DISPLAYD_HTML_H */
