(function() {
    // Respect "reduce motion": skip the animated WebGL background entirely
    // (CSS already hides the canvas, but without this the render loop would
    // keep running invisibly and burning battery).
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    // Skip the effect entirely on low-power / data-saver conditions. A live
    // full-screen WebGL background is pure decoration; on weak hardware or a
    // metered connection it costs more than it's worth and hurts scrolling.
    const nav = navigator;
    const lowPower =
        (nav.connection && nav.connection.saveData) ||
        (typeof nav.hardwareConcurrency === 'number' && nav.hardwareConcurrency <= 2) ||
        (typeof nav.deviceMemory === 'number' && nav.deviceMemory <= 2);
    if (lowPower) return;

    const c = document.createElement('canvas');
    c.className = 'mist-canvas';
    c.setAttribute('aria-hidden', 'true');
    document.body.prepend(c);
    const g = c.getContext('webgl', { alpha: false, antialias: false });
    if (!g) { c.remove(); return }
    const v = `attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}`;
    // Palette re-themed to bone/graphite + navy: neutral dark base, cool
    // slate mid-tone, muted navy highlight, subtle navy cursor glow.
    const f = `precision highp float;uniform float t;uniform vec2 r,m;float h(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}float n(vec2 p){vec2 i=floor(p),q=fract(p);float a=h(i),b=h(i+vec2(1.,0.)),c=h(i+vec2(0.,1.)),d=h(i+vec2(1.,1.));vec2 u=q*q*(3.-2.*q);return mix(a,b,u.x)+(c-a)*u.y*(1.-u.x)+(d-b)*u.x*u.y;}float fb(vec2 p){float x=0.,a=.5;for(int i=0;i<4;i++){x+=a*n(p);p*=2.;a*=.5;}return x;}void main(){vec2 u=gl_FragCoord.xy/r;u.x*=r.x/r.y;vec2 z=m/r;z.x*=r.x/r.y;float d=distance(u,z);vec2 q=vec2(fb(u+.07*t),fb(u+vec2(1.)));vec2 x=vec2(fb(u+q+vec2(1.7,9.2)+.15*t),fb(u+q+vec2(8.3,2.8)+.126*t));float k=fb(u+x);vec3 a=vec3(.028,.030,.036),b=vec3(.075,.090,.130),e=vec3(.19,.24,.38);vec3 col=mix(a,b,k);col=mix(col,e,dot(q,x)*.42);col+=smoothstep(.38,0.,d)*.035*vec3(.55,.68,1.0);col=pow(col,vec3(1.06))*1.20;gl_FragColor=vec4(col,1.);}`;

    function s(t, x) {
        const q = g.createShader(t);
        g.shaderSource(q, x);
        g.compileShader(q);
        return q
    }
    const p = g.createProgram();
    g.attachShader(p, s(g.VERTEX_SHADER, v));
    g.attachShader(p, s(g.FRAGMENT_SHADER, f));
    g.linkProgram(p);
    if (!g.getProgramParameter(p, g.LINK_STATUS)) { c.remove(); return }
    g.useProgram(p);
    const b = g.createBuffer();
    g.bindBuffer(g.ARRAY_BUFFER, b);
    g.bufferData(g.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), g.STATIC_DRAW);
    const a = g.getAttribLocation(p, 'p');
    g.enableVertexAttribArray(a);
    g.vertexAttribPointer(a, 2, g.FLOAT, false, 0, 0);
    const tl = g.getUniformLocation(p, 't'),
        rl = g.getUniformLocation(p, 'r'),
        ml = g.getUniformLocation(p, 'm');
    let mouse = { x: innerWidth * .55, y: innerHeight * .55 };

    function resize() {
        let d = 0.28;
        c.width = innerWidth * d;
        c.height = innerHeight * d;
        g.viewport(0, 0, c.width, c.height)
    }
    addEventListener('resize', resize, { passive: true });
    addEventListener('pointermove', e => {
        mouse.x = e.clientX;
        mouse.y = innerHeight - e.clientY
    }, { passive: true });
    resize();

    // ~30fps cap, and stop entirely while the tab is hidden.
    let last = 0, running = true;
    document.addEventListener('visibilitychange', () => {
        running = !document.hidden && !scrolling;
        if (running) requestAnimationFrame(draw);
    });

    // Pause while the user is actively scrolling. Re-blurring a full-screen
    // animated canvas behind the fixed, backdrop-blurred nav is the main cause
    // of scroll jank; freezing the background for a few hundred ms during a
    // scroll is imperceptible but frees the GPU to paint the page smoothly.
    let scrolling = false, scrollTimer = 0;
    addEventListener('scroll', () => {
        if (!scrolling && running) scrolling = true;   // stop drawing on next frame
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(() => {
            scrolling = false;
            if (!document.hidden) { running = true; requestAnimationFrame(draw); }
        }, 180);
    }, { passive: true });

    function draw(x) {
        if (!running || scrolling) return;
        if (x - last < 42) { requestAnimationFrame(draw); return }
        last = x;
        g.uniform1f(tl, x * .001);
        g.uniform2f(rl, c.width, c.height);
        g.uniform2f(ml, mouse.x * (c.width / innerWidth), mouse.y * (c.height / innerHeight));
        g.drawArrays(g.TRIANGLES, 0, 6);
        requestAnimationFrame(draw)
    }
    requestAnimationFrame(draw)
})();