# Building and publishing

## The .exe

The **Build Windows exe** workflow builds `ScreenBalance.exe` on every push: [Nuitka](https://nuitka.net) translates the Python code to C and compiles it into one native Windows program, then the workflow checks that it starts.

**Publish a new version:** open [Actions → Release](https://github.com/Catalyst-733/screen-balance/actions/workflows/release.yml) → **Run workflow**, and type the version (for example `v1.0.1`). Pushing a `v*` tag does the same. It builds the exe and attaches it to a GitHub Release.

**Build it yourself on Windows:** `pip install -r requirements.txt nuitka`, then run the `python -m nuitka ...` command from [`build-exe.yml`](../.github/workflows/build-exe.yml).

## The Microsoft Store package

The **Build Store package (MSIX)** workflow makes `ScreenBalance.msix`, installs it on the build machine and checks that it starts. The Store signs the package, so no code-signing certificate is needed.

1. Create a developer account at [Partner Center](https://partner.microsoft.com/dashboard) (Apps and games) and **reserve the name** "Screen Balance".
2. Under **Product management → Product identity**, copy *Package/Identity/Name*, *Package/Identity/Publisher* and *Package/Properties/PublisherDisplayName* into the repository variables `MSIX_IDENTITY_NAME`, `MSIX_PUBLISHER` and `MSIX_PUBLISHER_DISPLAY_NAME` (**Settings → Secrets and variables → Actions → Variables**). Without them the package uses test values the Store rejects.
3. Run **Build Store package (MSIX)** from the Actions tab with a version like `1.0.0.0` (raise it for each update; the last number stays `0`), and download **ScreenBalance-msix**.
4. Create a submission: upload the `.msix` under **Packages**, fill in the listing from [`store-listing.md`](store-listing.md), use [`PRIVACY.md`](../PRIVACY.md) as the privacy policy URL, and explain the `runFullTrust` capability with the text given there.

In the Store version, **Start with Windows** uses Windows' startup task instead of the registry (Store apps can't use the registry's startup list), so it also shows under **Settings → Apps → Startup**.

## Project layout

```
screen_balance.py      tracking, reminders, tray menu and windows
design.py              Notion-style drawing of every screen (Pillow, rendered at 2x)
tools/
  make_screenshots.py  regenerates the images in docs/ from demo data
  make_icon.py         draws the .exe icon and the Store logos
packaging/msix/        Store package manifest and its extra requirements
.github/workflows/
  build-exe.yml        compiles ScreenBalance.exe (Nuitka: Python -> C -> exe)
  build-msix.yml       builds ScreenBalance.msix for the Microsoft Store
  release.yml          publishes the exe as a GitHub Release
PRIVACY.md             privacy policy (linked from the Store listing)
```
