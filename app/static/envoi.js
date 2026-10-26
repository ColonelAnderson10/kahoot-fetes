// Aperçu des photos choisies et envoi en arrière-plan, plusieurs à la fois.
// - Chaque photo est réduite à 1920 px dans le navigateur avant l'envoi (beaucoup moins de données).
// - La réduction se fait une photo à la fois (mémoire du téléphone), l'envoi jusqu'à 3 à la fois.
// Tout le texte est inséré avec textContent (jamais innerHTML) pour éviter toute injection.
(function () {
  "use strict";

  const ENVOIS_SIMULTANES = 3;
  const COTE_MAX = 1920;
  const QUALITE_JPEG = 0.9;

  const formulaire = document.getElementById("form-photos");
  if (!formulaire) return;

  const champFichiers = document.getElementById("photos");
  const liste = document.getElementById("liste-photos");
  const progression = document.getElementById("progression");
  const barre = document.getElementById("barre");
  const etat = document.getElementById("etat");
  const zoneErreurs = document.getElementById("erreurs");
  const boutonEnvoyer = document.getElementById("envoyer");
  const csrf = formulaire.querySelector('input[name="csrf"]').value;
  const tailleMax = parseInt(formulaire.dataset.tailleMax, 10) * 1024 * 1024;

  // Photos choisies : { fichier, champDescription, element, statut, url }
  let selection = [];
  champFichiers.required = false;

  function creerElement(balise, classe, texte) {
    const el = document.createElement(balise);
    if (classe) el.className = classe;
    if (texte) el.textContent = texte;
    return el;
  }

  function changerStatut(entree, texte, classe) {
    entree.statut.textContent = texte;
    entree.element.classList.remove("en-cours", "envoyee", "echec");
    if (classe) entree.element.classList.add(classe);
  }

  function ajouterFichiers(fichiers) {
    for (const fichier of fichiers) {
      const element = creerElement("li", "photo-choisie");
      const apercu = creerElement("div", "apercu");
      const url = URL.createObjectURL(fichier);
      const img = document.createElement("img");
      img.alt = "";
      img.decoding = "async";
      img.loading = "lazy";
      img.src = url;
      img.addEventListener("error", function () {
        // Le HEIC ne s'affiche pas dans la plupart des navigateurs : il sera converti sur le serveur
        apercu.textContent = "";
        apercu.appendChild(creerElement("span", "sans-apercu", "📷 " + (fichier.name.split(".").pop() || "").toUpperCase()));
      });
      apercu.appendChild(img);

      const droite = creerElement("div", "details");
      droite.appendChild(creerElement("span", "nom-fichier", fichier.name));
      const champDescription = document.createElement("textarea");
      champDescription.rows = 2;
      champDescription.maxLength = 500;
      champDescription.placeholder = "Description (optionnelle)";
      champDescription.setAttribute("aria-label", "Description de " + fichier.name);
      droite.appendChild(champDescription);
      const statut = creerElement("span", "statut");
      droite.appendChild(statut);

      const retirer = creerElement("button", "retirer", "✕");
      retirer.type = "button";
      retirer.setAttribute("aria-label", "Retirer " + fichier.name);

      element.append(apercu, droite, retirer);
      liste.appendChild(element);
      const entree = { fichier, champDescription, element, statut, url, retirer };
      selection.push(entree);

      retirer.addEventListener("click", function () {
        URL.revokeObjectURL(url);
        element.remove();
        selection = selection.filter(function (e) { return e !== entree; });
      });
    }
  }

  champFichiers.addEventListener("change", function () {
    ajouterFichiers(champFichiers.files);
    champFichiers.value = ""; // permet d'ajouter d'autres photos ensuite
  });

  function afficherErreur(texte) {
    zoneErreurs.appendChild(creerElement("li", null, texte));
  }

  // --- Réduction dans le navigateur ---

  // Une seule réduction à la fois : décoder une photo de 48 Mpx prend ~200 Mo de mémoire
  let fileReduction = Promise.resolve();

  function reduire(fichier) {
    const tache = fileReduction.then(function () { return reduireMaintenant(fichier); });
    fileReduction = tache.catch(function () {});
    return tache;
  }

  async function reduireMaintenant(fichier) {
    if (!window.createImageBitmap || !HTMLCanvasElement.prototype.toBlob) return fichier;
    let image;
    try {
      // imageOrientation : applique la rotation EXIF avant de redessiner
      image = await createImageBitmap(fichier, { imageOrientation: "from-image" });
    } catch (e) {
      return fichier; // format non décodable par le navigateur (souvent HEIC) : le serveur s'en charge
    }
    try {
      const echelle = Math.min(1, COTE_MAX / Math.max(image.width, image.height));
      if (echelle === 1 && fichier.size <= 2 * 1024 * 1024) return fichier; // déjà petite
      const canevas = document.createElement("canvas");
      canevas.width = Math.round(image.width * echelle);
      canevas.height = Math.round(image.height * echelle);
      const contexte = canevas.getContext("2d");
      contexte.fillStyle = "#fff"; // fond blanc pour les PNG transparents
      contexte.fillRect(0, 0, canevas.width, canevas.height);
      contexte.drawImage(image, 0, 0, canevas.width, canevas.height);
      const blob = await new Promise(function (ok) { canevas.toBlob(ok, "image/jpeg", QUALITE_JPEG); });
      canevas.width = canevas.height = 0; // libère la mémoire tout de suite (iOS)
      if (!blob || blob.size >= fichier.size) return fichier;
      const nom = fichier.name.replace(/\.[^.]*$/, "") + ".jpg";
      return new File([blob], nom, { type: "image/jpeg" });
    } finally {
      image.close();
    }
  }

  // --- Envoi ---

  async function envoyerUne(entree) {
    changerStatut(entree, "Préparation…", "en-cours");
    const fichier = await reduire(entree.fichier);
    if (fichier.size > tailleMax) throw new Error("Trop volumineuse.");

    changerStatut(entree, "Envoi…", "en-cours");
    const donnees = new FormData();
    donnees.append("csrf", csrf);
    donnees.append("photos", fichier, fichier.name);
    donnees.append("descriptions", entree.champDescription.value);
    let reponse;
    for (let essai = 0; ; essai++) {
      reponse = await fetch("/photos", {
        method: "POST",
        body: donnees,
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      // Trop de requêtes : on patiente puis on réessaie (3 fois maximum)
      if (reponse.status !== 429 || essai >= 3) break;
      changerStatut(entree, "En pause…", "en-cours");
      await new Promise(function (ok) { setTimeout(ok, 15000); });
      changerStatut(entree, "Envoi…", "en-cours");
    }
    let resultat = {};
    try { resultat = await reponse.json(); } catch (e) { /* réponse non JSON (ex. 413) */ }
    if (!reponse.ok || !resultat.ok) {
      const message = (resultat.erreurs && resultat.erreurs.length)
        ? resultat.erreurs.join(" ")
        : (reponse.status === 413 ? "Trop volumineuse." : "Échec de l'envoi.");
      throw new Error(message);
    }
  }

  formulaire.addEventListener("submit", async function (evenement) {
    evenement.preventDefault();
    zoneErreurs.textContent = "";
    const aEnvoyer = selection.slice();
    if (!aEnvoyer.length) {
      afficherErreur("Choisis au moins une photo.");
      return;
    }

    boutonEnvoyer.disabled = true;
    champFichiers.disabled = true;
    aEnvoyer.forEach(function (e) {
      e.retirer.disabled = true;
      changerStatut(e, "En attente", null);
    });
    progression.hidden = false;
    barre.max = aEnvoyer.length;
    barre.value = 0;
    let terminees = 0;
    let reussies = 0;
    etat.textContent = "0 photo envoyée sur " + aEnvoyer.length + "…";

    // Quitter la page pendant l'envoi l'interromprait : on prévient
    function avertir(e) { e.preventDefault(); e.returnValue = ""; }
    window.addEventListener("beforeunload", avertir);

    // Petite file d'attente : ENVOIS_SIMULTANES travailleurs se partagent les photos
    let prochaine = 0;
    async function travailleur() {
      while (prochaine < aEnvoyer.length) {
        const entree = aEnvoyer[prochaine++];
        try {
          await envoyerUne(entree);
          reussies++;
          changerStatut(entree, "✓ Envoyée", "envoyee");
        } catch (erreur) {
          changerStatut(entree, "✗ " + erreur.message, "echec");
          afficherErreur(entree.fichier.name + " : " + erreur.message);
        }
        terminees++;
        barre.value = terminees;
        etat.textContent = reussies + " photo(s) envoyée(s) sur " + aEnvoyer.length + "…";
      }
    }
    const travailleurs = [];
    for (let i = 0; i < Math.min(ENVOIS_SIMULTANES, aEnvoyer.length); i++) travailleurs.push(travailleur());
    await Promise.all(travailleurs);
    window.removeEventListener("beforeunload", avertir);

    if (reussies === aEnvoyer.length) {
      window.location.assign("/merci?photos=" + reussies);
      return;
    }
    etat.textContent = reussies + " photo(s) envoyée(s) sur " + aEnvoyer.length + ".";
    // On retire de la liste celles qui sont parties pour pouvoir réessayer les autres
    selection = selection.filter(function (e) {
      if (e.element.classList.contains("envoyee")) {
        URL.revokeObjectURL(e.url);
        e.element.remove();
        return false;
      }
      e.retirer.disabled = false;
      return true;
    });
    boutonEnvoyer.disabled = false;
    champFichiers.disabled = false;
    boutonEnvoyer.textContent = "Réessayer";
    if (reussies > 0) {
      const lien = creerElement("a", "bouton bouton-second", "Terminer (" + reussies + " envoyée(s))");
      lien.href = "/merci?photos=" + reussies;
      zoneErreurs.appendChild(creerElement("li")).appendChild(lien);
    }
  });
})();
