#include "pil_container.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>

namespace
{
using namespace litehtml;

/* pixel_t has float and int constructors, so a double needs an explicit
 * narrowing step or the cast is ambiguous. */
pixel_t px(double v)
{
    return static_cast<pixel_t>(static_cast<float>(v));
}

/* w/h of 0 means "no clip" all the way down to the embedder. */
lhtml_rect NO_CLIP()
{
    lhtml_rect r = {0.0, 0.0, 0.0, 0.0};
    return r;
}

lhtml_color to_c(const web_color &c)
{
    lhtml_color out;
    out.r = c.red;
    out.g = c.green;
    out.b = c.blue;
    out.a = c.alpha;
    return out;
}

lhtml_rect to_c(const position &p)
{
    lhtml_rect r;
    r.x = static_cast<double>(p.x.value());
    r.y = static_cast<double>(p.y.value());
    r.w = static_cast<double>(p.width.value());
    r.h = static_cast<double>(p.height.value());
    return r;
}

int repeat_code(background_repeat r)
{
    switch(r)
    {
        case background_repeat_no_repeat: return LHTML_REPEAT_NO_REPEAT;
        case background_repeat_repeat_x:   return LHTML_REPEAT_REPEAT_X;
        case background_repeat_repeat_y:   return LHTML_REPEAT_REPEAT_Y;
        case background_repeat_repeat:
        default:                           return LHTML_REPEAT_REPEAT;
    }
}

/* Marker text. Anything not listed here falls back to a disc, which is the
 * documented degradation -- no crash, no missing glyph. */
std::string alpha_marker(int index, bool upper)
{
    std::string out;
    int         n = index;
    if(n <= 0)
    {
        return "0";
    }
    while(n > 0)
    {
        n--;
        out.insert(out.begin(), static_cast<char>('a' + (n % 26)));
        n /= 26;
    }
    if(upper)
    {
        for(auto &c : out)
        {
            c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        }
    }
    return out;
}

const char *const kRoman[] = {"",     "i",     "ii",     "iii",     "iv",    "v",     "vi",
                              "vii",  "viii",  "ix",     "x",      "xi",    "xii",   "xiii",
                              "xiv",  "xv",    "xvi",    "xvii",   "xviii", "xix",   "xx",
                              "xxi",  "xxii",  "xxiii",  "xxiv",   "xxv",   "xxvi",  "xxvii",
                              "xxviii", "xxix", "xxx",   "xxxi",   "xxxii", "xxxiii", "xxxiv"};

std::string roman_marker(int index, bool upper)
{
    int n = index;
    if(n <= 0 || n > 30)
    {
        return alpha_marker(index, upper);
    }
    std::string out = kRoman[n];
    if(upper)
    {
        for(auto &c : out)
        {
            c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        }
    }
    return out;
}

std::string marker_text(list_style_type type, int index)
{
    switch(type)
    {
        case list_style_type_decimal:             return std::to_string(index);
        case list_style_type_decimal_leading_zero:
        {
            std::string s = std::to_string(index);
            while(s.size() < 2)
            {
                s.insert(s.begin(), '0');
            }
            return s;
        }
        case list_style_type_lower_alpha:
        case list_style_type_lower_latin:  return alpha_marker(index, false);
        case list_style_type_upper_alpha:
        case list_style_type_upper_latin:  return alpha_marker(index, true);
        case list_style_type_lower_roman:  return roman_marker(index, false);
        case list_style_type_upper_roman:  return roman_marker(index, true);
        default:                           return {};
    }
}

} // namespace

pil_container::pil_container(const lhtml_callbacks *cb, int width, int height) :
    m_cb(cb), m_width(width), m_height(height)
{
}

lhtml_rect pil_container::clip_now() const
{
    if(m_clips.empty())
    {
        return NO_CLIP();
    }
    return to_c(m_clips.back());
}

lhtml_rect pil_container::clip_no() const
{
    return NO_CLIP();
}

lhtml_rect pil_container::clip_intersect(const position &pos) const
{
    lhtml_rect outer = clip_now();
    lhtml_rect inner = to_c(pos);
    if(outer.w <= 0.0 || outer.h <= 0.0)
    {
        return inner;
    }
    if(inner.w <= 0.0 || inner.h <= 0.0)
    {
        return NO_CLIP();
    }
    const double x0 = std::max(outer.x, inner.x);
    const double y0 = std::max(outer.y, inner.y);
    const double x1 = std::min(outer.x + outer.w, inner.x + inner.w);
    const double y1 = std::min(outer.y + outer.h, inner.y + inner.h);
    lhtml_rect   out;
    out.x = x0;
    out.y = y0;
    out.w = x1 > x0 ? x1 - x0 : 0.0;
    out.h = y1 > y0 ? y1 - y0 : 0.0;
    return out;
}

/* ------------------------------------------------------------------ fonts */

litehtml::uint_ptr pil_container::create_font(const font_description &descr, const document *,
                                             font_metrics *fm)
{
    if(m_cb == nullptr || m_cb->create_font == nullptr)
    {
        return 0;
    }
    lhtml_font_desc  desc;
    desc.family               = descr.family.c_str();
    desc.size                 = static_cast<double>(descr.size.value());
    desc.style                = (descr.style == font_style_italic) ? LHTML_FONT_ITALIC : 0;
    desc.weight               = descr.weight;
    desc.decoration           = 0;
    if(descr.decoration_line & text_decoration_line_underline)
    {
        desc.decoration |= LHTML_FONT_UNDERLINE;
    }
    if(descr.decoration_line & text_decoration_line_overline)
    {
        desc.decoration |= LHTML_FONT_OVERLINE;
    }
    if(descr.decoration_line & text_decoration_line_line_through)
    {
        desc.decoration |= LHTML_FONT_LINE_THROUGH;
    }
    desc.decoration_thickness = static_cast<double>(descr.decoration_thickness.val());
    desc.decoration_color     = to_c(descr.decoration_color);

    lhtml_font_metrics out;
    out.id        = 0;
    out.height    = 0.0;
    out.ascent    = 0.0;
    out.descent   = 0.0;
    out.x_height  = 0.0;
    out.ch_width  = 0.0;
    m_cb->create_font(m_cb->ctx, &desc, &out);
    if(out.id == 0)
    {
        return 0;
    }
    if(fm)
    {
        fm->font_size = descr.size;
        fm->height    = px(out.height);
        fm->ascent    = px(out.ascent);
        fm->descent   = px(out.descent);
        fm->x_height  = px(out.x_height);
        fm->ch_width  = px(out.ch_width);
        fm->draw_spaces = true;
    }
    return out.id;
}

void pil_container::delete_font(litehtml::uint_ptr hFont)
{
    if(m_cb != nullptr && m_cb->delete_font != nullptr)
    {
        m_cb->delete_font(m_cb->ctx, hFont);
    }
}

litehtml::pixel_t pil_container::text_width(const char *text, litehtml::uint_ptr hFont)
{
    if(m_cb == nullptr || m_cb->text_width == nullptr || text == nullptr || *text == '\0')
    {
        return 0_px;
    }
    return px(m_cb->text_width(m_cb->ctx, hFont, text));
}

/* ---------------------------------------------------------------- drawing */

void pil_container::draw_text(uint_ptr, const char *text, uint_ptr hFont, web_color color,
                              const position &pos)
{
    if(m_cb == nullptr || m_cb->draw_text == nullptr || text == nullptr || *text == '\0')
    {
        return;
    }
    m_cb->draw_text(m_cb->ctx, hFont, text, to_c(color), to_c(pos), clip_now());
}

void pil_container::draw_solid_fill(uint_ptr, const background_layer &layer, const web_color &color)
{
    if(m_cb == nullptr || m_cb->draw_fill == nullptr || color.alpha == 0)
    {
        return;
    }
    m_cb->draw_fill(m_cb->ctx, to_c(layer.border_box), clip_intersect(layer.clip_box), to_c(color));
}

void pil_container::draw_borders(uint_ptr, const borders &bs, const position &draw_pos, bool)
{
    if(m_cb == nullptr || m_cb->draw_borders == nullptr || !bs.is_visible())
    {
        return;
    }
    /* litehtml has already resolved the border geometry; we only flatten it
     * for the embedder. Radii are ignored (documented limitation). */
    lhtml_border_side sides[4];
    sides[LHTML_SIDE_LEFT]   = {bs.left.width.value(),   to_c(bs.left.color),   static_cast<int>(bs.left.style)};
    sides[LHTML_SIDE_TOP]    = {bs.top.width.value(),    to_c(bs.top.color),    static_cast<int>(bs.top.style)};
    sides[LHTML_SIDE_RIGHT]  = {bs.right.width.value(),  to_c(bs.right.color),  static_cast<int>(bs.right.style)};
    sides[LHTML_SIDE_BOTTOM] = {bs.bottom.width.value(), to_c(bs.bottom.color), static_cast<int>(bs.bottom.style)};
    m_cb->draw_borders(m_cb->ctx, to_c(draw_pos), clip_now(), sides);
}

void pil_container::draw_list_marker(uint_ptr, const list_marker &marker)
{
    if(m_cb == nullptr)
    {
        return;
    }
    const position &pos   = marker.pos;
    lhtml_color     color = to_c(marker.color);
    lhtml_rect     box   = to_c(pos);

    /* Shapes: one fill of the box litehtml reserved. `circle` renders as a
     * dot, the same as `disc` -- an outline needs the background colour,
     * which a container callback is not given. */
    switch(marker.marker_type)
    {
        case list_style_type_none:
            return;
        case list_style_type_circle:
        case list_style_type_disc:
        case list_style_type_square:
            if(m_cb->draw_fill != nullptr)
            {
                /* litehtml passes no clip for markers, and the engine draws
                 * them outside the li box -- to the left of it. Inheriting the
                 * last clip of an earlier element would drop the marker. */
                m_cb->draw_fill(m_cb->ctx, box, clip_no(), color);
            }
            return;
        default:
            break;
    }

    const std::string text = marker_text(marker.marker_type, marker.index);
    if(text.empty() || m_cb->draw_text == nullptr)
    {
        return;
    }
    m_cb->draw_text(m_cb->ctx, marker.font, text.c_str(), color, box, clip_no());
}

void pil_container::draw_linear_gradient(uint_ptr, const background_layer &, const background_layer::linear_gradient &)
{
}

void pil_container::draw_radial_gradient(uint_ptr, const background_layer &, const background_layer::radial_gradient &)
{
}

void pil_container::draw_conic_gradient(uint_ptr, const background_layer &, const background_layer::conic_gradient &)
{
}

/* ----------------------------------------------------------------- images */

void pil_container::load_image(const char *, const char *, bool)
{
    /* Nothing to do: the embedder resolves and decodes on demand inside
     * get_image_size()/draw_image(), so no URL is fetched at parse time. */
}

void pil_container::get_image_size(const char *src, const char *baseurl, size &sz)
{
    sz = size(0_px, 0_px);
    if(m_cb == nullptr || m_cb->image_size == nullptr || src == nullptr)
    {
        return;
    }
    double w = 0.0;
    double h = 0.0;
    m_cb->image_size(m_cb->ctx, src, baseurl, &w, &h);
    if(w > 0.0 && h > 0.0)
    {
        sz = size(px(w), px(h));
    }
}

void pil_container::draw_image(uint_ptr, const background_layer &layer, const std::string &url,
                               const std::string &base_url)
{
    if(m_cb == nullptr || m_cb->draw_image == nullptr)
    {
        return;
    }
    if(layer.origin_box.width <= 0_px || layer.origin_box.height <= 0_px)
    {
        return;
    }
    /* litehtml has already applied background-size, background-position and
     * the aspect ratio, so origin_box is the first tile and border_box is the
     * area that tile repeats across. Passing only origin_box draws exactly
     * one tile and looks like "backgrounds do not work". */
    m_cb->draw_image(m_cb->ctx, url.c_str(), base_url.c_str(), to_c(layer.origin_box),
                     to_c(layer.border_box), clip_intersect(layer.clip_box),
                     repeat_code(layer.repeat));
}

/* ------------------------------------------------------------- environment */

litehtml::pixel_t pil_container::pt_to_px(float pt) const
{
    /* CSS reference: 1pt = 1/72in, 96px = 1in. */
    return static_cast<pixel_t>(pt * 96.0f / 72.0f);
}

litehtml::pixel_t pil_container::get_default_font_size() const
{
    return 16_px;
}

const char *pil_container::get_default_font_name() const
{
    return "sans-serif";
}

void pil_container::get_viewport(position &viewport) const
{
    viewport = position(0_px, 0_px, static_cast<pixel_t>(m_width), static_cast<pixel_t>(m_height));
}

void pil_container::get_media_features(media_features &media) const
{
    media.type          = media_type_screen;
    media.width         = static_cast<pixel_t>(m_width);
    media.height        = static_cast<pixel_t>(m_height);
    media.device_width  = static_cast<pixel_t>(m_width);
    media.device_height = static_cast<pixel_t>(m_height);
    media.color         = 8;
    media.color_index   = 0;
    media.monochrome    = 0;
    media.resolution    = 96_px;
}

void pil_container::get_language(std::string &language, std::string &culture) const
{
    language = "en";
    culture  = "US";
}

void pil_container::transform_text(std::string &text, text_transform tt)
{
    switch(tt)
    {
        case text_transform_uppercase:
            for(auto &c : text)
            {
                c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
            }
            break;
        case text_transform_lowercase:
            for(auto &c : text)
            {
                c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
            }
            break;
        case text_transform_capitalize:
        {
            bool start = true;
            for(auto &c : text)
            {
                const unsigned char u = static_cast<unsigned char>(c);
                c = start ? static_cast<char>(std::toupper(u)) : static_cast<char>(std::tolower(u));
                start = (u == ' ' || u == '\t' || u == '\n' || u == '\r');
            }
            break;
        }
        case text_transform_none:
        default:
            break;
    }
}

void pil_container::set_clip(const position &pos, const border_radiuses &)
{
    m_clips.push_back(pos);
}

void pil_container::del_clip()
{
    if(!m_clips.empty())
    {
        m_clips.pop_back();
    }
}

litehtml::element::ptr pil_container::create_element(const char *, const litehtml::string_map &,
                                                     const std::shared_ptr<litehtml::document> &)
{
    /* No custom elements, so defer to litehtml's own factory, which maps
     * every tag it knows (body, div, style, table, img, br, ...) to the
     * right el_* class and then applies the attributes. Returning a generic
     * html_tag here would quietly downgrade <style> and <img> to inert
     * boxes, which is exactly the trap the reference container falls into. */
    return nullptr;
}

/* ------------------------------------------------------- refused features */

void pil_container::import_css(std::string &text, const std::string &, std::string &)
{
    /* Empty `text` means the <link>/@import contributes nothing. This is the
     * single choke point for external CSS: nothing else in litehtml fetches. */
    text.clear();
}

void pil_container::link(const std::shared_ptr<document> &, const element::ptr &)
{
}

void pil_container::on_anchor_click(const char *, const element::ptr &)
{
}

void pil_container::on_mouse_event(const element::ptr &, mouse_event)
{
}

void pil_container::set_cursor(const char *)
{
}

void pil_container::set_caption(const char *)
{
}

void pil_container::set_base_url(const char *)
{
}
