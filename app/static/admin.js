// Administration : filtres appliqués dès qu'on change une liste, confirmation avant suppression.
(function () {
  "use strict";

  const filtres = document.querySelector(".filtres");
  if (filtres) {
    filtres.querySelectorAll("select").forEach(function (liste) {
      liste.addEventListener("change", function () {
        filtres.submit();
      });
    });
  }

  document.querySelectorAll(".form-supprimer").forEach(function (formulaire) {
    formulaire.addEventListener("submit", function (evenement) {
      if (!window.confirm("Supprimer définitivement cette soumission ?")) {
        evenement.preventDefault();
      }
    });
  });
})();
