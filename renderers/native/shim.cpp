/*
 * extern "C" entry points for the displayd html renderer.
 *
 * One call = one complete frame: parse, lay out, draw, tear down. Nothing
 * survives the call, so a caller that renders 1000 times holds 1000 times
 * zero extra memory on the C++ side. The document is destroyed before
 * returning, which is what triggers delete_font for every font it created.
 *
 * Every entry point is exception-tight: a litehtml throw becomes an
 * LHTML_ERR_* code and a message, never an unwind into ctypes.
 */
#include <cstring>
#include <exception>
#include <string>

#include <litehtml.h>

#include "displayd_html.h"
#include "pil_container.h"

namespace
{
#ifndef LITEHTML_PINNED_REV
#define LITEHTML_PINNED_REV "unknown"
#endif

void set_err(char *err, int errlen, const char *what)
{
    if(err == nullptr || errlen <= 0)
    {
        return;
    }
    std::snprintf(err, static_cast<size_t>(errlen), "%s", what ? what : "");
}

void set_err(char *err, int errlen, const char *what, const char *detail)
{
    if(err == nullptr || errlen <= 0)
    {
        return;
    }
    std::snprintf(err, static_cast<size_t>(errlen), "%s: %s", what ? what : "",
                  detail ? detail : "");
}

} // namespace

extern "C" const char *lhtml_version(void)
{
    return "litehtml " LITEHTML_PINNED_REV " (displayd pil container, c-abi 1)";
}

extern "C" int lhtml_render(const lhtml_callbacks *cb, const char *html, int width, int height,
                            double *out_content_height, char *err, int errlen)
{
    if(out_content_height)
    {
        *out_content_height = 0.0;
    }
    if(err != nullptr && errlen > 0)
    {
        err[0] = '\0';
    }
    if(cb == nullptr || html == nullptr || *html == '\0')
    {
        set_err(err, errlen, "no document to render");
        return LHTML_ERR_MISSING;
    }
    if(width <= 0 || height <= 0)
    {
        set_err(err, errlen, "render size must be positive");
        return LHTML_ERR_MISSING;
    }

    try
    {
        pil_container container(cb, width, height);

        litehtml::document::ptr doc =
          litehtml::document::createFromString(std::string(html), &container);
        if(!doc)
        {
            set_err(err, errlen, "litehtml returned no document");
            return LHTML_ERR_PARSE;
        }

        /* render() lays out and returns the *natural width* it found, which is
         * not a height. The laid-out document height only lands in m_size once
         * calc_document_size() has run inside render(), so read it back from
         * the document accessor rather than trusting the return value. (This
         * revision declares content_height() but never defines it, so height()
         * is the accessor that actually links.) */
        doc->render(static_cast<litehtml::pixel_t>(width));
        litehtml::pixel_t content_h = doc->height();
        if(content_h <= litehtml::pixel_t(0))
        {
            /* Nothing laid out: an empty or whitespace-only document. Still a
             * valid frame, so report success with the viewport height. */
            content_h = static_cast<litehtml::pixel_t>(height);
        }

        litehtml::position clip(litehtml::pixel_t(0), litehtml::pixel_t(0), static_cast<litehtml::pixel_t>(width),
                                static_cast<litehtml::pixel_t>(height));
        doc->draw(0, litehtml::pixel_t(0), litehtml::pixel_t(0), &clip);

        if(out_content_height)
        {
            *out_content_height = static_cast<double>(content_h.value());
        }
        /* doc goes out of scope here: ~document() calls delete_font for
         * every entry in its font map, which is the whole reason fonts are
         * flat across renders. */
        return LHTML_OK;
    }
    catch(const std::exception &e)
    {
        set_err(err, errlen, "litehtml error", e.what());
    }
    catch(...)
    {
        set_err(err, errlen, "litehtml error", "unknown exception");
    }
    return LHTML_ERR_PARSE;
}
