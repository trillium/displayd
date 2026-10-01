/*
 * A litehtml document_container that lays out in C++ and paints through the
 * embedder (PIL, over the C ABI in displayd_html.h).
 *
 * Deliberately narrow. Every method that would need a browser -- gradients,
 * remote CSS, scripting, cursors, hit testing -- is an explicit no-op, so
 * the supported subset is exactly what is implemented here plus the shared
 * layout code. See docs/HTML_RENDERER.md for the contract this implements.
 *
 * The reference container in litehtml's own containers/test/ is deliberately
 * NOT reused: it is stale against current headers (MSVC-isms, pixel_t
 * treated as int) and its delete_font is a no-op, which leaks every raster
 * font. This one honours delete_font so steady-state memory is flat.
 */
#ifndef DISPLAYD_PIL_CONTAINER_H
#define DISPLAYD_PIL_CONTAINER_H

#include <litehtml.h>

#include <string>
#include <vector>

#include "displayd_html.h"

class pil_container : public litehtml::document_container
{
  public:
    pil_container(const lhtml_callbacks *cb, int width, int height);

    /* Fonts. Metrics are real measurements supplied by the embedder, because
     * litehtml resolves em/ex/ch units and line box heights from them. */
    litehtml::uint_ptr create_font(const litehtml::font_description &descr,
                                   const litehtml::document *doc,
                                   litehtml::font_metrics *fm) override;
    void               delete_font(litehtml::uint_ptr hFont) override;
    litehtml::pixel_t  text_width(const char *text,
                                  litehtml::uint_ptr hFont) override;

    /* Text and shapes. */
    void draw_text(litehtml::uint_ptr hdc, const char *text, litehtml::uint_ptr hFont,
                   litehtml::web_color color, const litehtml::position &pos) override;
    void draw_solid_fill(litehtml::uint_ptr hdc, const litehtml::background_layer &layer,
                         const litehtml::web_color &color) override;
    void draw_borders(litehtml::uint_ptr hdc, const litehtml::borders &borders,
                      const litehtml::position &draw_pos, bool root) override;
    void draw_list_marker(litehtml::uint_ptr hdc, const litehtml::list_marker &marker) override;
    /* Unsupported by contract: a gradient background paints nothing. */
    void draw_linear_gradient(litehtml::uint_ptr hdc, const litehtml::background_layer &layer,
                              const litehtml::background_layer::linear_gradient &gradient) override;
    void draw_radial_gradient(litehtml::uint_ptr hdc, const litehtml::background_layer &layer,
                              const litehtml::background_layer::radial_gradient &gradient) override;
    void draw_conic_gradient(litehtml::uint_ptr hdc, const litehtml::background_layer &layer,
                             const litehtml::background_layer::conic_gradient &gradient) override;

    /* Images. The embedder owns decoding and the cache; we only report the
     * size during layout and the geometry at draw time. */
    void load_image(const char *src, const char *baseurl, bool redraw_on_ready) override;
    void get_image_size(const char *src, const char *baseurl, litehtml::size &sz) override;
    void draw_image(litehtml::uint_ptr hdc, const litehtml::background_layer &layer,
                    const std::string &url, const std::string &base_url) override;

    /* Environment. */
    litehtml::pixel_t     pt_to_px(float pt) const override;
    litehtml::pixel_t     get_default_font_size() const override;
    const char           *get_default_font_name() const override;
    void                  get_viewport(litehtml::position &viewport) const override;
    void                  get_media_features(litehtml::media_features &media) const override;
    void                  get_language(std::string &language, std::string &culture) const override;
    void                  transform_text(std::string &text, litehtml::text_transform tt) override;
    void                  set_clip(const litehtml::position &pos,
                                   const litehtml::border_radiuses &bdr_radius) override;
    void                  del_clip() override;
    litehtml::element::ptr create_element(const char *tag_name,
                                          const litehtml::string_map &attributes,
                                          const std::shared_ptr<litehtml::document> &doc) override;

    /* Remote and interactive features, refused by contract. import_css()
     * leaves `text` empty, so <link rel=stylesheet> and @import contribute
     * nothing and no URL is ever dereferenced. */
    void import_css(std::string &text, const std::string &url, std::string &baseurl) override;
    void link(const std::shared_ptr<litehtml::document> &doc,
              const litehtml::element::ptr &el) override;
    void on_anchor_click(const char *url, const litehtml::element::ptr &el) override;
    void on_mouse_event(const litehtml::element::ptr &el,
                        litehtml::mouse_event event) override;
    void set_cursor(const char *cursor) override;
    void set_caption(const char *caption) override;
    void set_base_url(const char *base_url) override;

  private:
    const lhtml_callbacks *m_cb;
    int                    m_width;
    int                    m_height;
    std::vector<litehtml::position> m_clips;

    lhtml_rect clip_now() const;
    lhtml_rect clip_no() const;
    lhtml_rect clip_intersect(const litehtml::position &pos) const;
};

#endif /* DISPLAYD_PIL_CONTAINER_H */
