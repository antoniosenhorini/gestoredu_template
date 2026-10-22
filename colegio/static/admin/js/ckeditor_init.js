document.addEventListener('DOMContentLoaded', function() {
    if (document.getElementById('editor')) {
        CKEDITOR.replace('editor', {
            toolbar: 'Full',
            height: 400,
            width: '100%'
        });
    }
});
