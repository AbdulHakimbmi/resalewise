const toast = document.querySelector('.toast');
let timer;

function showToast(msg) {
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(timer);
    timer = setTimeout(() => toast.classList.remove('show'), 3200)
}
document.querySelectorAll('[data-toast]').forEach(e => e.addEventListener('click', () => showToast(e.dataset.toast)));
document.querySelectorAll('[data-demo-form]').forEach(f => f.addEventListener('submit', e => {
    e.preventDefault();
    showToast(f.dataset.demoForm || 'Prototype action completed. Connect it to PHP and your database later.')
}));
document.querySelectorAll('.rate').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('.rate').forEach(x => x.classList.remove('active'));
    b.classList.add('active')
}));
document.querySelectorAll('[data-save]').forEach(b => b.addEventListener('click', () => {
    const saved = !b.dataset.saved;
    b.dataset.saved = saved ? '1' : '';
    b.textContent = saved ? 'Saved' : 'Save';
    showToast(saved ? 'Saved to your shortlist.' : 'Removed from shortlist.')
}));