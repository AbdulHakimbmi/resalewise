/*
  ResaleWise SG — town coverflow carousel with local image support

  Put your images in: assets/images/towns/
  Example: assets/images/towns/ang-mo-kio.jpg

  Filenames must be lowercase, hyphenated, .jpg
  (e.g. ANG MO KIO -> ang-mo-kio.jpg, KALLANG/WHAMPOA -> kallang-whampoa.jpg)

  This version supports BOTH HTML structures:

  A) Existing card with a hidden or existing <img> tag:
     <article class="town-slide" data-town="ANG MO KIO">
       <img src="..." alt="...">
       ...
     </article>

  B) Existing card with no <img> tag:
     <article class="town-slide" data-town="ANG MO KIO">
       ...
     </article>
*/

(function() {
    const root = document.querySelector("[data-coverflow]");

    if (!root) {
        return;
    }

    const stage = root.querySelector(".coverflow-stage");
    const slides = [...root.querySelectorAll(".town-slide")];
    const dots = [...root.querySelectorAll(".dot")];
    const caption = root.querySelector("[data-town-caption]");
    const previousButton = root.querySelector(".prev");
    const nextButton = root.querySelector(".next");
    const townJump = document.getElementById("townJump");

    let position = 0;
    let target = 0;
    let drag = null;
    let animationFrame = null;

    const count = slides.length;

    if (!stage || count === 0) {
        return;
    }

    /*
      Figure out the site's base URL from this script's own <script src>.
      Example:
        script src = http://localhost/resalewise-sg/assets/js/town-coverflow.js
        base       = http://localhost/resalewise-sg/
      Works for file:// too.
    */
    function getScriptBase() {
        const scripts = document.getElementsByTagName("script");
        for (let i = 0; i < scripts.length; i++) {
            const src = scripts[i].src;
            if (src && src.indexOf("town-coverflow.js") !== -1) {
                return src.replace(/assets\/js\/town-coverflow\.js.*$/, "");
            }
        }
        return "";
    }

    const scriptBase = getScriptBase();

    console.info("[town-coverflow] scriptBase =", scriptBase || "(not detected, using relative paths)");

    function townSlug(townName) {
        return townName
            .toLowerCase()
            .replace(/\//g, "-")
            .replace(/\s+/g, "-")
            .replace(/[^a-z0-9-]/g, "")
            .replace(/-+/g, "-");
    }

    function imagePathForTown(townName) {
        return scriptBase + "assets/images/towns/" + townSlug(townName) + ".jpg";
    }

    function loadTownImage(slide) {
        const town = slide.dataset.town;

        if (!town) {
            return;
        }

        let image = slide.querySelector("img");

        if (!image) {
            image = document.createElement("img");
            slide.prepend(image);
        }

        const path = imagePathForTown(town);

        image.alt = "HDB estate view of " + town;
        image.loading = "eager";
        image.decoding = "async";

        image.addEventListener("load", function() {
            slide.classList.add("has-image");
            console.info("[town-coverflow] Loaded:", image.src);
        });

        image.addEventListener("error", function() {
            image.remove();
            slide.classList.remove("has-image");
            console.warn("[town-coverflow] FAILED to load:", path);
        });

        image.src = path;
    }

    slides.forEach(loadTownImage);

    function shortestOffset(offset) {
        offset = ((offset % count) + count) % count;

        if (offset > count / 2) {
            offset -= count;
        }

        return offset;
    }

    function cardWidth() {
        return Math.min(Math.max(255, root.clientWidth * 0.25), 365);
    }

    function currentIndex() {
        return ((Math.round(position) % count) + count) % count;
    }

    function paint() {
        const width = cardWidth();
        const pitch = width * 0.67;

        slides.forEach(function(slide, index) {
            const offset = shortestOffset(index - position);
            const distance = Math.abs(offset);
            const ramp = Math.pow(distance, 0.56);
            const rotation = Math.min(43 * ramp, 80) * Math.sign(offset);

            slide.style.transform =
                "translate(-50%, -50%) " +
                "translateX(" + (offset * pitch) + "px) " +
                "translateZ(" + (-width * 0.62 * ramp) + "px) " +
                "rotateY(" + (-rotation) + "deg)";

            slide.style.opacity = Math.max(0.09, 1 - 0.115 * distance);
            slide.style.zIndex = String(100 - Math.round(distance));
            slide.classList.toggle("active", Math.abs(offset) < 0.5);
        });

        const index = currentIndex();
        const selectedSlide = slides[index];

        dots.forEach(function(dot, dotIndex) {
            dot.classList.toggle("active", dotIndex === index);
        });

        if (caption && selectedSlide) {
            caption.innerHTML =
                "<strong>" + selectedSlide.dataset.town + "</strong>" +
                "<span>" + (selectedSlide.dataset.caption || "") + "</span>";
        }

        if (townJump && selectedSlide) {
            townJump.value = selectedSlide.dataset.town;
        }
    }

    function settle() {
        if (animationFrame !== null) {
            cancelAnimationFrame(animationFrame);
        }

        function step() {
            const remaining = target - position;

            if (Math.abs(remaining) < 0.001) {
                position = target;
                paint();
                animationFrame = null;
                return;
            }

            position += remaining * 0.15;
            paint();
            animationFrame = requestAnimationFrame(step);
        }

        animationFrame = requestAnimationFrame(step);
    }

    function goTo(index) {
        target = index + Math.round((target - index) / count) * count;
        settle();
    }

    function moveBy(amount) {
        target = Math.round(target) + amount;
        settle();
    }

    stage.addEventListener("pointerdown", function(event) {
        stage.setPointerCapture(event.pointerId);

        // Remember which slide the press started on, so a tap (no drag) can
        // navigate to it from endDrag — this works even though the stage
        // captures the pointer (which otherwise suppresses child click events).
        var slideEl = event.target.closest ? event.target.closest(".town-slide") : null;

        drag = {
            id: event.pointerId,
            startX: event.clientX,
            startPosition: position,
            velocity: 0,
            time: performance.now(),
            slide: slideEl,
            slideIndex: slideEl ? slides.indexOf(slideEl) : -1
        };

        if (animationFrame !== null) {
            cancelAnimationFrame(animationFrame);
            animationFrame = null;
        }
    });

    stage.addEventListener("pointermove", function(event) {
        if (!drag || drag.id !== event.pointerId) {
            return;
        }

        const now = performance.now();
        const previousPosition = position;

        position =
            drag.startPosition -
            (event.clientX - drag.startX) / (cardWidth() * 0.67);

        drag.velocity =
            ((position - previousPosition) / Math.max(now - drag.time, 1)) * 1000;

        drag.time = now;
        target = position;
        paint();
    });

    function endDrag(event) {
        if (!drag || drag.id !== event.pointerId) {
            return;
        }

        const moved = Math.abs(event.clientX - drag.startX) > 6;
        const tappedSlide = drag.slide;
        const tappedIndex = drag.slideIndex;

        const momentum = Math.max(-2, Math.min(2, drag.velocity * 0.18));
        target = Math.round(position + momentum);
        drag = null;
        settle();

        // A tap (no meaningful drag) on a slide → navigate to that town.
        if (!moved && tappedSlide) {
            const town = tappedSlide.dataset.town;
            if (town) {
                // If it wasn't the centre card, centre it first for feedback,
                // then navigate; otherwise navigate immediately.
                if (tappedIndex !== currentIndex()) {
                    goTo(tappedIndex);
                    setTimeout(function() {
                        location.href = "town.html?town=" + encodeURIComponent(town);
                    }, 240);
                } else {
                    location.href = "town.html?town=" + encodeURIComponent(town);
                }
            }
        }
    }

    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    stage.addEventListener("keydown", function(event) {
        if (event.key === "ArrowLeft") {
            event.preventDefault();
            moveBy(-1);
        }

        if (event.key === "ArrowRight") {
            event.preventDefault();
            moveBy(1);
        }
    });

    if (previousButton) {
        previousButton.addEventListener("click", function() {
            moveBy(-1);
        });
    }

    if (nextButton) {
        nextButton.addEventListener("click", function() {
            moveBy(1);
        });
    }

    dots.forEach(function(dot, index) {
        dot.addEventListener("click", function() {
            goTo(index);
        });
    });

    // Navigation on tap is handled in endDrag (pointer capture makes a
    // separate click listener unreliable), so no click handler is needed here.

    if (townJump) {
        townJump.addEventListener("change", function() {
            const index = slides.findIndex(function(slide) {
                return slide.dataset.town === townJump.value;
            });

            if (index >= 0) {
                goTo(index);
            }
        });
    }

    window.addEventListener("resize", paint, { passive: true });

    paint();
})();