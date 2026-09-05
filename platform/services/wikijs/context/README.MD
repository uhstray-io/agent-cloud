# Setup your Wiki.js Site

This is legacy application context, not a supported platform deployment procedure.
The former direct Docker command enabled application-managed TLS and bypassed
Semaphore/OpenBao. Any platform integration must declare deployment through
Semaphore and terminate public TLS at Caddy.

The UI customization notes below are historical reference, not config-as-code automation.

# Enable youtube videos to be embeded on your Wiki.js Site
- [Article and source material](https://github.com/requarks/wiki/discussions/4580)

1. Log in in as an admin to your wikijs site

    ![admin panel navigation](.images/admin_settings.png)

2. Select Theme from the admin panel

    ![theme selection](.images/navigate_to_theme.png)

3. Add code to "CSS OVERRIDE" and "Head HTML Injection" Sections

    ![add code](.images/code_injection.png)

4. Add youtube link to your wiki page

    ![add youtube link](.images/test_link.png)

5. Embeded youtube video

    ![embeded youtube video](.images/embeded_video.png)
