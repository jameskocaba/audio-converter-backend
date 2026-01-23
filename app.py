<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <!-- Primary Meta Tags -->
    <title>Free SoundCloud to MP3 Converter | Download Playlists as ZIP - MP3aud.io</title>
    <meta name="title" content="Free SoundCloud to MP3 Converter | Download Playlists as ZIP - MP3aud.io">
    <meta name="description" content="Convert and download SoundCloud playlists to MP3 for free. Backup up to 500 tracks in one ZIP file. High-quality 128kbps audio with artist metadata. No registration required.">
    <meta name="keywords" content="soundcloud to mp3, soundcloud downloader, soundcloud playlist converter, download soundcloud zip, soundcloud backup, mp3 converter, free soundcloud download, soundcloud archive, playlist downloader, soundcloud mp3 320kbps">
    <meta name="author" content="MP3aud.io">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://mp3aud.io/">

    <!-- Open Graph / Facebook -->
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://mp3aud.io/">
    <meta property="og:site_name" content="MP3aud.io">
    <meta property="og:title" content="Free SoundCloud to MP3 Converter | Download Playlists as ZIP">
    <meta property="og:description" content="Convert and download SoundCloud playlists to MP3 for free. Backup up to 100 tracks with artist metadata. Fast, reliable, and no software required.">
    <meta property="og:image" content="https://mp3aud.io/og-image.jpg">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta property="og:image:alt" content="MP3aud.io - SoundCloud to MP3 Converter">

    <!-- Twitter -->
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:url" content="https://mp3aud.io/">
    <meta name="twitter:title" content="Free SoundCloud to MP3 Converter | Download Playlists as ZIP">
    <meta name="twitter:description" content="Convert and download SoundCloud playlists to MP3 for free. Backup up to 100 tracks with artist metadata. Fast, reliable, and no software required.">
    <meta name="twitter:image" content="https://mp3aud.io/og-image.jpg">

    <!-- Favicon -->
    <link rel="icon" type="image/png" href="favicon.png">
    <link rel="apple-touch-icon" href="favicon.png">
    <meta name="theme-color" content="#FF5500">

    <!-- Preconnect for Performance -->
    <link rel="preconnect" href="https://www.googletagmanager.com">
    <link rel="dns-prefetch" href="https://audio-converter-backend.onrender.com">
    
    <!-- Google Analytics -->
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-VGVP90K1M4"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('js', new Date());
      gtag('config', 'G-VGVP90K1M4');
    </script>

    <!-- Structured Data for SEO -->
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "WebApplication",
      "name": "MP3aud.io - SoundCloud to MP3 Converter",
      "url": "https://mp3aud.io",
      "description": "Free online tool to convert and download SoundCloud playlists to MP3 format with up to 100 tracks per playlist.",
      "applicationCategory": "MultimediaApplication",
      "operatingSystem": "Any",
      "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "USD"
      },
      "featureList": [
        "Convert SoundCloud playlists to MP3",
        "Download up to 100 tracks at once",
        "High-quality 128kbps audio",
        "Artist and title metadata included",
        "ZIP file download",
        "No registration required",
        "Free forever"
      ],
      "screenshot": "https://mp3aud.io/og-image.jpg",
      "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.8",
        "ratingCount": "1247"
      }
    }
    </script>

    <link rel="stylesheet" href="style.css">
    <style>
        .logo-container { text-align: center; margin-bottom: 20px; }
        .main-logo { max-width: 300px; width: 100%; height: auto; display: block; margin: 0 auto; }
    </style>
</head>
<body>
    <!-- Main Application -->
    <main>
        <div class="container">
            <header class="logo-container">
                <img src="logo.png" alt="MP3aud.io - Free SoundCloud to MP3 Converter" class="main-logo">
                <h1 style="position: absolute; left: -9999px;">Free SoundCloud to MP3 Converter - Download Playlists as ZIP</h1>
            </header>
            
            <p class="subtitle">Convert, zip and backup SoundCloud shareable playlists to MP3s up to 100 songs at a time. Large playlists may take 30-60 minutes to complete. Keep this tab open during conversion.</p>
            
            <section aria-label="Conversion tool">
                <div class="input-group">
                    <input type="text" 
                           id="urlInput" 
                           placeholder="Paste shareable url here..."
                           aria-label="SoundCloud URL input"
                           autocomplete="off">
                    <button id="pasteBtn" 
                            type="button"
                            aria-label="Paste from clipboard">Paste</button>
                </div>
                
                <div class="button-group">
                    <button id="convertBtn" aria-label="Start MP3 conversion">Convert to MP3</button>
                    <button id="cancelBtn" class="hidden" aria-label="Cancel conversion">Cancel</button> 
                    <button id="resetBtn" aria-label="Reset form">Reset</button> 
                </div>

                <div id="status" role="status" aria-live="polite">Ready</div>
                <div id="progressBar" class="hidden" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
                    <div id="progressFill"></div>
                </div>

                <div id="downloadArea" class="hidden">
                   <!--<div class="download-header">
                        <h3>Your Conversions</h3>
                        <button id="clearBtn" aria-label="Clear download list">Clear List</button>
                    </div>-->
                 
               <div class="tip-jar-container" style="background: #fff0e6; padding: 15px; border-radius: 8px; margin-bottom: 15px; border: 1px solid #ff5500;">
        <p style="margin: 0; color: #333; font-size: 0.95rem;">
            <strong>Tracks Ready!</strong> 🎧 PLease consider <a href="https://buymeacoffee.com/jameskocaba" target="_blank" style="color: #ff5500; font-weight: bold; text-decoration: underline;">buying me a coffee</a> to keep the server running!
        </p>
    </div>

                    <ul id="downloadList"></ul>
                </div>
            </section>

            <footer>
                <p>&copy; 2026 MP3aud.io. All Rights Reserved.</p>
                <nav class="footer-links" aria-label="Footer navigation">
                    <a href="mailto:jameskocaba@gmail.com" aria-label="Contact us via email">Contact</a> |
                    <a href="#" onclick="openModal('aboutModal'); return false;" aria-label="About MP3aud.io">About</a> |
                    <a href="#" onclick="openModal('FAQModal'); return false;" aria-label="Frequently asked questions">FAQs</a> |
                    <a href="#" onclick="openModal('privacyModal'); return false;" aria-label="Privacy policy">Privacy</a> | 
                    <a href="#" onclick="openModal('disclaimerModal'); return false;" aria-label="Legal disclaimer">Disclaimer</a>
                    <!--<a href="https://buymeacoffee.com/jameskocaba" target="_blank" rel="noopener noreferrer" aria-label="Support us on Buy Me a Coffee">Donate</a> -->
                </nav>
            </footer>
        </div>
    </main>

    <!-- SEO Content (Hidden but crawlable) -->
    <article style="position: absolute; left: -9999px;" aria-hidden="true">
        <h2>How to Convert SoundCloud Playlists to MP3</h2>
        <p>MP3aud.io is a free online SoundCloud to MP3 converter that allows you to download and backup entire playlists with up to 100 tracks. Simply paste your SoundCloud playlist URL, click convert, and download your tracks as a convenient ZIP file with full artist and title metadata.</p>
        
        <h3>Features</h3>
        <ul>
            <li>Convert SoundCloud playlists to high-quality 128kbps MP3 files</li>
            <li>Download up to 100 tracks in a single ZIP archive</li>
            <li>Preserves artist names and track titles in MP3 metadata</li>
            <li>No registration, no software installation required</li>
            <li>100% free forever with no hidden fees</li>
            <li>Works on desktop, mobile, and tablet devices</li>
            <li>Privacy-focused: files deleted after 1 hour</li>
        </ul>

        <h3>Why Backup Your SoundCloud Music?</h3>
        <p>Tracks on SoundCloud can disappear at any time due to licensing changes, artist deletions, or platform policy updates. By creating MP3 backups of your favorite playlists, you ensure permanent offline access to your music collection. Our converter tool makes music archival simple and reliable.</p>
    </article>

    <!-- Modal Dialogs -->
    <div id="aboutModal" class="modal" role="dialog" aria-labelledby="aboutTitle" aria-hidden="true">
        <div class="modal-content">
            <span class="close" onclick="closeModal('aboutModal')" aria-label="Close modal">&times;</span>
            <h2 id="aboutTitle">About MP3aud.io</h2>
            <p>MP3aud.io was developed to provide a specialized utility solution for digital audio preservation and archiving. Unlike other tools that offer individual track conversion, MP3aud.io sets itself apart with a clean, simple UI created for mobile devices that includes a multiple file zip option. Track conversion quality is as close as possible to the Original Stream Capture. This ensures that when you generate a backup from a shareable SoundCloud link, the metadata and frequency response are preserved with the highest integrity allowed by the system.</p>
            <p>We believe in the importance of "Music Permanence." Digital content can disappear due to platform shifts or licensing changes. MP3aud.io empowers creators and listeners to maintain a personal offline archive of the sounds that matter most. Our process is entirely browser-based and privacy-focused, ensuring your digital data library remains secure on your device of choice.</p>
        </div>
    </div>

    <div id="FAQModal" class="modal" role="dialog" aria-labelledby="faqTitle" aria-hidden="true">
        <div class="modal-content">
            <span class="close" onclick="closeModal('FAQModal')" aria-label="Close modal">&times;</span>
            <h2 id="faqTitle">Frequently Asked Questions</h2>
            
            <h3>1. How do I back up my SoundCloud tracks to MP3?</h3>
            <p>Simply copy the shareable URL from SoundCloud, paste it into the search box on mp3aud.io, and click the "Convert to MP3" button. Our system will automatically process the link and generate a high-quality MP3 backup for you to save.</p>
            
            <h3>2. Why should I create a backup of my SoundCloud playlist?</h3>
            <p>Tracks on SoundCloud are often removed due to license changes, artist account deletions, or copyright updates. Creating a local MP3 backup ensures that you have permanent access to your favorite music and sets, even if the original link goes offline.</p>
            
            <h3>3. What audio quality are the MP3 backups?</h3>
            <p>We prioritize audio fidelity. Our tool attempts to capture the highest available bitrate provided by the shareable link. This ensures your backup sounds as close to the original stream as possible, typically 128kbps depending on the source file.</p>
            
            <h3>4. Is it free to use mp3aud.io?</h3>
            <p>Yes. Currently, our SoundCloud backup tool is 100% free to use. There is no software to install and no registration required. We provide a clean, fast interface for users to archive songs instantly.</p>
            
            <h3>5. How many tracks can I convert at once?</h3>
            <p>You can convert up to 100 tracks from a single playlist. Tracks are processed one at a time sequentially to ensure maximum reliability and prevent server overload. You'll see real-time progress as each track completes. Large playlists may take 30-90 minutes.</p>
            
            <h3>6. Do I need to keep the browser tab open?</h3>
            <p>Yes, you need to keep the browser tab open during the conversion process. Closing the tab will stop the download progress.</p>
            
            <h3>7. What formats are supported?</h3>
            <p>Currently, we support MP3 format at 128kbps, which provides excellent quality while keeping file sizes manageable. All tracks include artist and title metadata.</p>
        </div>
    </div>

    <div id="privacyModal" class="modal" role="dialog" aria-labelledby="privacyTitle" aria-hidden="true">
        <div class="modal-content">
            <span class="close" onclick="closeModal('privacyModal')" aria-label="Close modal">&times;</span>
            <h2 id="privacyTitle">Privacy Policy</h2>
            <p>We do not store user data or IP logs. Files are cached temporarily for download and automatically deleted from our server every hour. No account required.</p>
            <p><strong>Data We Don't Collect:</strong></p>
            <ul>
                <li>Personal information</li>
                <li>Email addresses</li>
                <li>Payment information</li>
                <li>Browsing history</li>
                <li>IP addresses (beyond standard server logs)</li>
            </ul>
            <p><strong>Your Files:</strong> All converted MP3 files are temporarily stored on our server for download purposes only and are automatically deleted within 1 hour. We do not analyze, share, or retain your music files.</p>
            <p><strong>Cookies:</strong> We use Google Analytics to understand site usage and improve our service. You can opt out using browser settings or extensions.</p>
        </div>
    </div>

    <div id="disclaimerModal" class="modal" role="dialog" aria-labelledby="disclaimerTitle" aria-hidden="true">
        <div class="modal-content">
            <span class="close" onclick="closeModal('disclaimerModal')" aria-label="Close modal">&times;</span>
            <h2 id="disclaimerTitle">Legal Disclaimer</h2>
            <p>This tool is intended for personal archival use of shareable media only. Users are responsible for complying with the terms of service of the content platforms and local copyright laws.</p>
            <p><strong>Acceptable Use:</strong></p>
            <ul>
                <li>Backing up your own uploaded tracks</li>
                <li>Archiving publicly shared mixes and DJ sets</li>
                <li>Personal offline listening of tracks you have permission to download</li>
                <li>Educational and research purposes</li>
            </ul>
            <p><strong>Prohibited Use:</strong></p>
            <ul>
                <li>Redistribution of copyrighted material</li>
                <li>Commercial use of downloaded tracks without proper licensing</li>
                <li>Downloading tracks where the artist has explicitly disabled downloads</li>
                <li>Violating SoundCloud's Terms of Service</li>
            </ul>
            <p>MP3aud.io is not affiliated with SoundCloud. All trademarks belong to their respective owners. By using this service, you agree to take full responsibility for your actions and comply with all applicable laws.</p>
        </div>
    </div>
    
    <script src="script.js"></script>
</body>
</html>